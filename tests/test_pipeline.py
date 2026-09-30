from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from cmdb.db import create_db_engine, init_db
from cmdb.ingest.errors import IngestError, MappingGenerationError
from cmdb.ingest.mapping import MappingContext, MappingSpec
from cmdb.ingest.pipeline import IngestAI, ingest_file
from cmdb.ingest.resolver import EntitySummary, MatchDecision
from cmdb.models import (
    App,
    CIIdentifier,
    CIRelationship,
    CIType,
    Device,
    IngestionRun,
    IngestionStatus,
    SchemaMapping,
    User,
)
from fakes import SAMPLE_FILES, FakeClassifier, fake_ingest_ai, sample_bytes


def count(session: Session, model: type) -> int:
    return session.scalar(select(func.count()).select_from(model))


def rel_counts(session: Session) -> dict[str, int]:
    rows = session.execute(
        select(CIRelationship.rel_type, func.count()).group_by(CIRelationship.rel_type)
    )
    return {rel_type.value: n for rel_type, n in rows}


def one(session: Session, model: type, **filters: Any) -> Any:
    return session.scalars(select(model).filter_by(**filters)).one()


def ingest(session: Session, ai: IngestAI | None, filename: str, **kwargs: Any) -> Any:
    return ingest_file(session, ai, filename=filename, content=sample_bytes(filename), **kwargs)


# --- the sample data, end to end ---


def test_entity_counts(ingested: Session) -> None:
    assert count(ingested, Device) == 3
    assert count(ingested, User) == 9
    assert count(ingested, App) == 28
    assert rel_counts(ingested) == {"OWNS": 3, "USES": 25, "INTEGRATES_WITH": 16}


def test_conflicting_device_values_are_resolved(ingested: Session) -> None:
    john = one(ingested, Device, hostname="laptop-jdoe")
    jane = one(ingested, Device, hostname="macbook-cto-01")
    sarah = one(ingested, Device, hostname="laptop-devops")

    # London (CSV 7/22, YAML 7/23) beats New York HQ (JSON 6/12); YAML's "deactivated" is newest.
    assert (john.location, john.status, john.os_family, john.os_version) == (
        "London",
        "inactive",
        "macOS",
        "13.5",
    )
    # The undated JSON "inactive" loses to the dated YAML "active".
    assert (jane.status, jane.location) == ("active", "New York HQ")
    assert (sarah.os_family, sarah.os_version) == ("Ubuntu", "22.04")


def test_identities_are_merged_across_sources(ingested: Session) -> None:
    john = one(ingested, User, name="John Doe")
    identifiers = {(i.kind.value, i.source_system, i.value) for i in john.identifiers}

    assert identifiers == {
        ("email", "", "john.d@example.com"),
        ("email", "", "john.doe@example.com"),
        ("employee_id", "", "emp-2156"),
        ("external_id", "okta", "u_999"),
    }
    assert john.email == "john.doe@example.com"  # the dated YAML beats undated Okta
    assert john.mfa_enabled is True
    assert john.team == "Platform Engineering"
    owner = ingested.execute(
        text("SELECT owner_name FROM v_devices WHERE hostname = 'laptop-jdoe'")
    ).scalar()
    assert owner == "John Doe"


def test_app_stubs_are_merged_into_inventory_records(ingested: Session) -> None:
    github = one(ingested, App, name="GitHub Enterprise")
    observed = ingested.execute(
        text("SELECT observed_user_count FROM v_apps WHERE name = 'GitHub Enterprise'")
    ).scalar()

    assert github.is_stub is False
    assert github.reported_usage_count == 28
    assert observed == 5
    assert one(ingested, App, name="Salesforce CRM").is_stub is False
    assert one(ingested, App, name="Jira").is_stub is True
    assert ingested.scalar(select(func.count()).select_from(App).where(App.name == "GitHub")) == 0


def snapshot(session: Session) -> tuple[Any, ...]:
    devices = sorted(
        tuple(row)
        for row in session.execute(
            text(
                "SELECT hostname, status, location, os_family, os_version, ip_address, "
                "mac_address, encryption_enabled, owner_name FROM v_devices"
            )
        )
    )
    users = sorted(
        (
            u.name,
            u.email,
            u.status,
            u.mfa_enabled,
            u.department,
            u.team,
            tuple(sorted((i.kind.value, i.source_system, i.value) for i in u.identifiers)),
        )
        for u in session.scalars(select(User))
    )
    apps = sorted((a.name, a.is_stub, a.app_type) for a in session.scalars(select(App)))
    relationships = sorted(
        tuple(row)
        for row in session.execute(
            text("SELECT source_name, rel_type, target_name FROM v_relationships")
        )
    )
    return devices, users, apps, relationships


ORDERS = [
    SAMPLE_FILES,
    list(reversed(SAMPLE_FILES)),
    [
        "sample_hardware.yaml",
        "sample_okta.json",
        "sample_hardware.csv",
        "sample_app.json",
        "sample_hardware.json",
    ],
]


def test_results_do_not_depend_on_upload_order(tmp_path: Path) -> None:
    snapshots = []
    for index, order in enumerate(ORDERS):
        engine = create_db_engine(f"sqlite:///{(tmp_path / f'order{index}.db').as_posix()}")
        init_db(engine)
        with Session(engine) as session:
            ai = fake_ingest_ai()
            for filename in order:
                ingest(session, ai, filename)
            snapshots.append(snapshot(session))
        engine.dispose()

    assert snapshots[0] == snapshots[1] == snapshots[2]


def test_reingesting_changes_nothing(ingested: Session, ingest_ai: IngestAI) -> None:
    before = snapshot(ingested)

    for filename in SAMPLE_FILES:
        report = ingest(ingested, ingest_ai, filename)
        assert all(c.created == 0 and c.updated == 0 for c in report.cis.values()), filename
        assert report.relationships_created == {}

    assert snapshot(ingested) == before


def test_known_formats_reuse_the_cached_mapping(ingested: Session, ingest_ai: IngestAI) -> None:
    calls_before = len(ingest_ai.mapper.calls)

    report = ingest(ingested, ingest_ai, "sample_okta.json")

    assert report.mapping_source == "cache"
    assert len(ingest_ai.mapper.calls) == calls_before


def test_first_ingest_reports_the_mapping_decision(session: Session, ingest_ai: IngestAI) -> None:
    report = ingest(session, ingest_ai, "sample_okta.json")

    assert report.mapping_source == "llm"
    assert report.source_system == "okta"
    assert report.ai_decisions[0]["type"] == "schema_mapping"
    assert report.cis["user"].created == 9
    assert report.relationships_created == {"USES": 25}


# --- options and failures ---


def test_explicit_source_system_overrides_the_mapping(
    session: Session, ingest_ai: IngestAI
) -> None:
    report = ingest(session, ingest_ai, "sample_okta.json", source_system="Okta Prod")

    assert report.source_system == "okta_prod"
    systems = session.scalars(
        select(CIIdentifier.source_system).where(CIIdentifier.kind == "external_id").distinct()
    ).all()
    assert systems == ["okta_prod"]


CSV_HEADER = sample_bytes("sample_hardware.csv").splitlines()[0]
REPAIR_ROW = (
    b"X-1,laptop-x,Pat Q.,macOS 14,10.0.0.9,AA:BB:CC:00:00:01,2024-07-22T10:00:00Z,"
    b"London,FileVault 2,In Repair,laptop,SER-X1,IT"
)


def test_unrecognized_values_are_classified(session: Session) -> None:
    classifier = FakeClassifier({("status", "In Repair"): "inactive"})
    ai = fake_ingest_ai(classifier=classifier)

    report = ingest_file(
        session, ai, filename="sample_hardware.csv", content=CSV_HEADER + b"\n" + REPAIR_ROW
    )

    assert one(session, Device, hostname="laptop-x").status == "inactive"
    assert [item.raw for item in classifier.items] == ["In Repair"]
    decision = report.ai_decisions[-1]
    assert (decision["type"], decision["answer"], decision["accepted"]) == (
        "value_classification",
        "inactive",
        True,
    )


def test_unclassifiable_values_are_kept_in_attributes(session: Session) -> None:
    ai = fake_ingest_ai(classifier=FakeClassifier({}))

    report = ingest_file(
        session, ai, filename="sample_hardware.csv", content=CSV_HEADER + b"\n" + REPAIR_ROW
    )

    laptop = one(session, Device, hostname="laptop-x")
    assert laptop.status == "unknown"
    assert laptop.attributes["status"] == "In Repair"
    assert any("could not interpret status" in w for w in report.warnings)


def test_unparseable_file_fails_the_run(session: Session, ingest_ai: IngestAI) -> None:
    with pytest.raises(IngestError) as caught:
        ingest_file(session, ingest_ai, filename="broken.json", content=b'{"a": ')

    assert caught.value.status_code == 422
    run = session.get(IngestionRun, caught.value.ingestion_id)
    assert run.status is IngestionStatus.FAILED
    assert "Invalid JSON" in run.error
    assert count(session, User) == 0


UNKNOWN_FORMAT = b'[{"asset": "srv-01", "rack": "B2"}]'


def test_new_format_without_ai_is_refused(session: Session) -> None:
    with pytest.raises(IngestError) as caught:
        ingest_file(session, None, filename="racks.json", content=UNKNOWN_FORMAT)

    assert caught.value.status_code == 503
    assert session.get(IngestionRun, caught.value.ingestion_id).status is IngestionStatus.FAILED


def test_new_format_with_a_failing_llm_is_a_502(session: Session) -> None:
    ai = IngestAI(mapper=FailingMapper())

    with pytest.raises(IngestError) as caught:
        ingest_file(session, ai, filename="racks.json", content=UNKNOWN_FORMAT)

    assert caught.value.status_code == 502


class FailingMapper:
    def generate(self, context: MappingContext) -> MappingSpec:
        raise MappingGenerationError("model unavailable")


class InvalidMapper:
    def generate(self, context: MappingContext) -> MappingSpec:
        return MappingSpec(ci_type=CIType.USER, fields={"name": "no_such_path"})


def test_sample_files_ingest_without_ai_using_bundled_mappings(session: Session) -> None:
    reports = [ingest(session, None, filename) for filename in SAMPLE_FILES]

    assert {r.mapping_source for r in reports} == {"bundled"}
    assert all(r.status is IngestionStatus.COMPLETED for r in reports)
    assert count(session, Device) == 3
    assert rel_counts(session)["OWNS"] == 3
    # Bundled mappings are not cached, so a configured LLM can still map these formats later.
    assert count(session, SchemaMapping) == 0


@pytest.mark.parametrize("mapper", [FailingMapper(), InvalidMapper()])
def test_llm_mapping_failure_falls_back_to_the_bundled_mapping(
    session: Session, mapper: Any
) -> None:
    report = ingest(session, IngestAI(mapper=mapper), "sample_okta.json")

    assert report.mapping_source == "bundled"
    assert report.cis["user"].created == 9
    assert any("used the bundled mapping" in w for w in report.warnings)
    assert report.ai_decisions[0]["type"] == "schema_mapping"
    assert "error" in report.ai_decisions[0]
    assert count(session, SchemaMapping) == 0


def test_known_format_works_without_ai(ingested: Session) -> None:
    report = ingest(ingested, None, "sample_hardware.csv")

    assert report.status is IngestionStatus.COMPLETED
    assert report.mapping_source == "cache"


def test_a_failure_mid_run_rolls_back_everything(session: Session, ingest_ai: IngestAI) -> None:
    ingest(session, ingest_ai, "sample_okta.json")

    class ExplodingJudge:
        def judge(
            self, incoming: EntitySummary, candidates: list[EntitySummary], *, context: str
        ) -> MatchDecision:
            raise RuntimeError("boom")

    ai = IngestAI(mapper=ingest_ai.mapper, classifier=None, judge=ExplodingJudge())
    with pytest.raises(IngestError) as caught:
        ingest(session, ai, "sample_hardware.csv")

    assert caught.value.status_code == 500
    run = session.get(IngestionRun, caught.value.ingestion_id)
    assert run.status is IngestionStatus.FAILED
    assert "boom" in run.error
    assert count(session, Device) == 0  # no half-ingested file
    assert rel_counts(session) == {"USES": 25}
