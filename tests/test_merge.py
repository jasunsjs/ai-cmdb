from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cmdb.ingest.canonical import normalize_record
from cmdb.ingest.merge import (
    Observation,
    attach_identifiers,
    create_ci,
    current_owner,
    link,
    merge_ci,
    takes_precedence,
)
from cmdb.ingest.report import RunNotes
from cmdb.models import App, CIIdentifier, CIRelationship, CIType, Device, RelType, User


def dated(day: int, record_id: int = 1) -> Observation:
    return Observation(datetime(2024, 7, day, tzinfo=UTC), record_id)


UNDATED = Observation(None, 1)


def device(**values: Any) -> Any:
    return normalize_record(CIType.DEVICE, values)


def new_device(session: Session, obs: Observation, **values: Any) -> Device:
    return create_ci(session, device(hostname="laptop-a", **values), {}, obs)


@pytest.mark.parametrize(
    ("provenance", "current", "obs", "expected"),
    [
        (None, None, UNDATED, True),  # anything fills a blank
        ({"observed_at": None}, "x", UNDATED, False),  # undated never overrides
        ({"observed_at": None}, "x", dated(1), True),  # dated beats undated
        ({"observed_at": "2024-07-10T00:00:00+00:00"}, "x", dated(20), True),  # newer wins
        ({"observed_at": "2024-07-10T00:00:00+00:00"}, "x", dated(5), False),  # older loses
        ({"observed_at": "2024-07-10T00:00:00+00:00"}, "x", dated(10), True),  # tie: incoming
    ],
)
def test_takes_precedence(
    provenance: dict[str, Any] | None, current: Any, obs: Observation, expected: bool
) -> None:
    assert takes_precedence(provenance, current, obs) is expected


def test_create_records_values_and_provenance(session: Session) -> None:
    laptop = create_ci(
        session, device(hostname="laptop-a", location="London"), {"cpu": "M2"}, dated(22, 7)
    )

    assert (laptop.name, laptop.location, laptop.attributes) == (
        "laptop-a",
        "London",
        {"cpu": "M2"},
    )
    assert laptop.field_provenance["location"] == {
        "observed_at": "2024-07-22T00:00:00+00:00",
        "source_record_id": 7,
    }
    assert "attributes.cpu" in laptop.field_provenance


def test_newer_observation_wins_and_older_only_fills_blanks(session: Session) -> None:
    laptop = new_device(session, dated(22), location="London")

    assert merge_ci(laptop, device(location="New York HQ", ip_address="10.0.0.1"), {}, dated(12),
                    full_record=True)  # fmt: skip
    assert laptop.location == "London"  # older value lost
    assert laptop.ip_address == "10.0.0.1"  # but filled the blank

    merge_ci(laptop, device(location="Paris"), {}, dated(23), full_record=True)
    assert laptop.location == "Paris"


def test_undated_observations_only_fill_blanks(session: Session) -> None:
    laptop = new_device(session, dated(22), status="active")

    merge_ci(laptop, device(status="inactive", location="London"), {}, UNDATED, full_record=True)

    assert laptop.status == "active"
    assert laptop.location == "London"


def test_unobserved_defaults_count_as_blank(session: Session) -> None:
    # Created without a status: it holds the default "unknown", which was never observed.
    laptop = new_device(session, dated(22))
    assert laptop.status == "unknown"

    merge_ci(laptop, device(status="active"), {}, UNDATED, full_record=True)

    assert laptop.status == "active"


def test_same_values_change_nothing(session: Session) -> None:
    laptop = new_device(session, dated(22), location="London")
    session.commit()
    before = laptop.updated_at

    changed = merge_ci(laptop, device(hostname="laptop-a", location="London"), {}, dated(23),
                       full_record=True)  # fmt: skip
    session.commit()

    assert changed is False
    assert laptop.updated_at == before


@pytest.mark.parametrize(("first", "second"), [("John D.", "John Doe"), ("John Doe", "John D.")])
def test_the_more_complete_user_name_wins_in_any_order(
    session: Session, first: str, second: str
) -> None:
    john = create_ci(session, normalize_record(CIType.USER, {"name": first}), {}, dated(1))

    merge_ci(john, normalize_record(CIType.USER, {"name": second}), {}, UNDATED, full_record=True)

    assert john.name == "John Doe"


def test_a_full_record_promotes_a_stub_app(session: Session) -> None:
    app = create_ci(session, normalize_record(CIType.APP, {"name": "Jira"}), {}, UNDATED, stub=True)
    assert app.is_stub

    merge_ci(app, normalize_record(CIType.APP, {"name": "Jira"}), {}, UNDATED, full_record=False)
    assert app.is_stub  # a mere reference doesn't promote it

    merge_ci(app, normalize_record(CIType.APP, {"name": "Jira"}), {}, UNDATED, full_record=True)
    assert not app.is_stub


def test_attach_identifiers(session: Session) -> None:
    notes = RunNotes()
    john = create_ci(session, normalize_record(CIType.USER, {"name": "John"}), {}, UNDATED)
    jane = create_ci(session, normalize_record(CIType.USER, {"name": "Jane"}), {}, UNDATED)
    record = normalize_record(
        CIType.USER, {"email": "john@example.com", "external_id": "u_1", "employee_id": "E1"}
    )

    assert attach_identifiers(session, john, record, source_system="okta", notes=notes)
    assert not attach_identifiers(session, john, record, source_system="okta", notes=notes)

    stolen = normalize_record(CIType.USER, {"email": "john@example.com"})
    assert not attach_identifiers(session, jane, stolen, source_system=None, notes=notes)
    assert "already belongs to CI" in notes.warnings[-1]

    unscoped = normalize_record(CIType.USER, {"external_id": "u_2"})
    assert not attach_identifiers(session, jane, unscoped, source_system=None, notes=notes)
    assert "source system is unknown" in notes.warnings[-1]

    rows = session.execute(
        select(CIIdentifier.kind, CIIdentifier.source_system).where(CIIdentifier.ci_id == john.id)
    ).all()
    assert sorted(rows) == [("email", ""), ("employee_id", ""), ("external_id", "okta")]


def test_owner_is_replaced_only_by_a_newer_observation(session: Session) -> None:
    notes = RunNotes()
    laptop = new_device(session, dated(20))
    jane = create_ci(session, normalize_record(CIType.USER, {"name": "Jane"}), {}, UNDATED)
    john = create_ci(session, normalize_record(CIType.USER, {"name": "John"}), {}, UNDATED)

    def own(owner: User, obs: Observation) -> bool:
        return link(session, owner, laptop, RelType.OWNS, confidence=1.0, ingestion_id=None,
                    obs=obs, notes=notes)  # fmt: skip

    assert own(jane, dated(20))
    assert not own(john, dated(10))  # older: kept Jane
    assert current_owner(session, laptop) == jane
    assert "kept its current owner" in notes.warnings[-1]

    assert own(john, dated(25))  # newer: replaced
    assert current_owner(session, laptop) == john
    owns = select(func.count()).select_from(CIRelationship).where(CIRelationship.rel_type == "OWNS")
    assert session.scalar(owns) == 1


def test_link_ignores_duplicates_and_self_links(session: Session) -> None:
    notes = RunNotes()
    slack = create_ci(session, normalize_record(CIType.APP, {"name": "Slack"}), {}, UNDATED)
    jira = create_ci(session, normalize_record(CIType.APP, {"name": "Jira"}), {}, UNDATED)
    args = {"confidence": 1.0, "ingestion_id": None, "obs": UNDATED, "notes": notes}

    assert link(session, slack, jira, RelType.INTEGRATES_WITH, **args)
    assert not link(session, slack, jira, RelType.INTEGRATES_WITH, **args)
    assert not link(session, slack, slack, RelType.INTEGRATES_WITH, **args)
    assert notes.relationships_created == {"INTEGRATES_WITH": 1}


def test_fallback_names(session: Session) -> None:
    unnamed_device = create_ci(
        session, normalize_record(CIType.DEVICE, {"asset_tag": "C-1"}), {}, UNDATED
    )
    unnamed_user = create_ci(
        session, normalize_record(CIType.USER, {"email": "pat@example.com"}), {}, UNDATED
    )

    assert unnamed_device.name == "c-1"
    assert unnamed_user.name == "pat"
    assert isinstance(unnamed_device, Device) and isinstance(unnamed_user, User)
    assert not isinstance(unnamed_user, App)
