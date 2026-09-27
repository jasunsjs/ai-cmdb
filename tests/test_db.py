from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, inspect, select, text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from cmdb.db import create_db_engine, init_db
from cmdb.models import (
    App,
    CIIdentifier,
    CIRelationship,
    CIStatus,
    CIType,
    ConfigurationItem,
    Device,
    IdentifierKind,
    IngestionRun,
    RelType,
    SourceRecord,
    User,
)

EXPECTED_TABLES = {
    "configuration_items",
    "devices",
    "users",
    "apps",
    "relationships",
    "ci_identifiers",
    "source_records",
    "ingestion_runs",
    "schema_mappings",
}
EXPECTED_VIEWS = {
    "v_devices",
    "v_users",
    "v_apps",
    "v_user_apps",
    "v_app_integrations",
    "v_relationships",
}


def count(session: Session, table: str) -> int:
    return session.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()


def link(session: Session, source: ConfigurationItem, target: ConfigurationItem, rel: RelType):
    session.add(CIRelationship(source=source, target=target, rel_type=rel))
    session.flush()


# --- engine and schema setup ---


def test_creates_db_file_in_missing_directory(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "dir" / "cmdb.db"
    engine = create_db_engine(f"sqlite:///{db_path.as_posix()}")
    init_db(engine)
    engine.dispose()

    assert db_path.exists()


def test_all_tables_and_views_exist(engine: Engine) -> None:
    inspector = inspect(engine)

    assert set(inspector.get_table_names()) == EXPECTED_TABLES
    assert set(inspector.get_view_names()) == EXPECTED_VIEWS


def test_sqlite_pragmas_are_set(session: Session) -> None:
    assert session.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
    assert session.execute(text("PRAGMA journal_mode")).scalar_one() == "wal"


def test_init_db_is_idempotent(engine: Engine) -> None:
    init_db(engine)
    init_db(engine)

    assert set(inspect(engine).get_view_names()) == EXPECTED_VIEWS


def test_app_startup_initializes_database(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}
    assert "configuration_items" in inspect(client.app.state.engine).get_table_names()


# --- joined-table inheritance and column types ---


def test_device_round_trips_polymorphically(session: Session) -> None:
    device = Device(name="laptop-jdoe", hostname="laptop-jdoe", status=CIStatus.ACTIVE)
    session.add(device)
    session.commit()
    device_id = device.id
    session.expunge_all()

    loaded = session.get(ConfigurationItem, device_id)

    assert isinstance(loaded, Device)
    assert loaded.ci_type is CIType.DEVICE
    assert loaded.ci_id == loaded.id
    assert count(session, "configuration_items") == 1
    assert count(session, "devices") == 1
    assert count(session, "users") == 0


def test_enums_are_stored_by_value(session: Session) -> None:
    session.add(Device(name="d1", status=CIStatus.INACTIVE))
    session.commit()

    row = session.execute(text("SELECT ci_type, status FROM configuration_items")).one()

    assert tuple(row) == ("device", "inactive")


def test_invalid_enum_value_is_rejected_by_check_constraint(session: Session) -> None:
    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO configuration_items "
                "(ci_type, name, status, attributes, field_provenance, created_at, updated_at) "
                "VALUES ('device', 'x', 'bogus', '{}', '{}', '2024-01-01', '2024-01-01')"
            )
        )


def test_datetimes_round_trip_as_utc_aware(session: Session) -> None:
    plus_two = timezone(timedelta(hours=2))
    user = User(name="Jane Doe", last_login=datetime(2024, 7, 15, 12, 3, tzinfo=plus_two))
    session.add(user)
    session.commit()
    user_id = user.id
    session.expunge_all()

    loaded = session.get(User, user_id)

    assert loaded.last_login == datetime(2024, 7, 15, 10, 3, tzinfo=UTC)
    assert loaded.last_login.tzinfo is UTC
    assert loaded.created_at.tzinfo is UTC


LONG_AGO = datetime(2020, 1, 1, tzinfo=UTC)


def stored_updated_at(session: Session, ci_id: int) -> datetime:
    session.expire_all()
    return session.get(ConfigurationItem, ci_id).updated_at


def test_updated_at_changes_when_only_a_subclass_column_changes(session: Session) -> None:
    device = Device(name="laptop", location="New York HQ", updated_at=LONG_AGO)
    session.add(device)
    session.commit()

    device.location = "London"  # lives in `devices`, not `configuration_items`
    session.commit()

    assert stored_updated_at(session, device.id) > LONG_AGO


def test_updated_at_changes_when_a_base_column_changes(session: Session) -> None:
    device = Device(name="laptop", updated_at=LONG_AGO)
    session.add(device)
    session.commit()

    device.attributes["cpu"] = "Apple M2 Pro"
    session.commit()

    assert stored_updated_at(session, device.id) > LONG_AGO


def test_updated_at_unchanged_when_nothing_changes(session: Session) -> None:
    device = Device(name="laptop", location="London", updated_at=LONG_AGO)
    session.add(device)
    session.commit()
    # Load current values first: commit() expires attributes, and assigning to an expired
    # attribute counts as a change because SQLAlchemy can't compare against the old value.
    session.refresh(device)

    device.location = "London"  # same value: no net change
    session.commit()

    assert stored_updated_at(session, device.id) == LONG_AGO


def test_naive_datetime_is_rejected(session: Session) -> None:
    session.add(User(name="Jane Doe", last_login=datetime(2024, 7, 15, 10, 3)))

    with pytest.raises(StatementError, match="Naive datetime"):
        session.commit()


def test_in_place_json_changes_are_persisted(session: Session) -> None:
    device = Device(name="d1")
    session.add(device)
    session.commit()

    device.attributes["cpu"] = "Apple M2 Pro"
    device.field_provenance["location"] = {"source_record_id": 1}
    session.commit()
    device_id = device.id
    session.expunge_all()

    loaded = session.get(Device, device_id)
    assert loaded.attributes == {"cpu": "Apple M2 Pro"}
    assert loaded.field_provenance == {"location": {"source_record_id": 1}}


# --- constraints ---


def test_users_with_the_same_name_are_allowed(session: Session) -> None:
    first = User(name="John Doe", email="john.doe@example.com")
    second = User(name="John Doe", email="jdoe@example.com")
    session.add_all([first, second])
    session.flush()
    session.add_all(
        [
            CIIdentifier(ci=first, kind=IdentifierKind.EMAIL, value="john.doe@example.com"),
            CIIdentifier(ci=second, kind=IdentifierKind.EMAIL, value="jdoe@example.com"),
        ]
    )
    session.commit()

    assert count(session, "users") == 2


def test_identifier_can_belong_to_only_one_ci(session: Session) -> None:
    first, second = User(name="A"), User(name="B")
    session.add_all([first, second])
    session.flush()
    session.add(CIIdentifier(ci=first, kind=IdentifierKind.EMAIL, value="a@example.com"))
    session.flush()
    session.add(CIIdentifier(ci=second, kind=IdentifierKind.EMAIL, value="a@example.com"))

    with pytest.raises(IntegrityError):
        session.flush()


def test_same_external_id_from_different_systems_is_allowed(session: Session) -> None:
    okta_user, jira_user = User(name="Jane Doe"), User(name="Build Bot")
    session.add_all([okta_user, jira_user])
    session.flush()
    session.add_all(
        [
            CIIdentifier(
                ci=okta_user, kind=IdentifierKind.EXTERNAL_ID, source_system="okta", value="u_110"
            ),
            CIIdentifier(
                ci=jira_user, kind=IdentifierKind.EXTERNAL_ID, source_system="jira", value="u_110"
            ),
        ]
    )
    session.commit()

    assert count(session, "ci_identifiers") == 2


def test_same_external_id_from_the_same_system_is_rejected(session: Session) -> None:
    first, second = User(name="A"), User(name="B")
    session.add_all([first, second])
    session.flush()
    for ci in (first, second):
        session.add(
            CIIdentifier(ci=ci, kind=IdentifierKind.EXTERNAL_ID, source_system="okta", value="u_1")
        )

    with pytest.raises(IntegrityError):
        session.flush()


@pytest.mark.parametrize(
    ("kind", "source_system"),
    [
        (IdentifierKind.EXTERNAL_ID, ""),  # system-issued ids must name their system
        (IdentifierKind.EMAIL, "okta"),  # org-wide keys must not be scoped
    ],
)
def test_identifier_source_system_scope_is_enforced(
    session: Session, kind: IdentifierKind, source_system: str
) -> None:
    user = User(name="A")
    session.add(user)
    session.flush()
    session.add(CIIdentifier(ci=user, kind=kind, source_system=source_system, value="x"))

    with pytest.raises(IntegrityError):
        session.flush()


def test_duplicate_relationship_is_rejected(session: Session) -> None:
    user, app = User(name="Jane"), App(name="Slack")
    session.add_all([user, app])
    link(session, user, app, RelType.USES)

    with pytest.raises(IntegrityError):
        link(session, user, app, RelType.USES)


def test_self_relationship_is_rejected(session: Session) -> None:
    app = App(name="Slack")
    session.add(app)

    with pytest.raises(IntegrityError):
        link(session, app, app, RelType.INTEGRATES_WITH)


def test_confidence_must_be_between_zero_and_one(session: Session) -> None:
    user, app = User(name="Jane"), App(name="Slack")
    session.add_all([user, app])
    session.add(CIRelationship(source=user, target=app, rel_type=RelType.USES, confidence=1.5))

    with pytest.raises(IntegrityError):
        session.flush()


def test_device_can_have_only_one_owner(session: Session) -> None:
    jane, john, laptop = User(name="Jane"), User(name="John"), Device(name="laptop")
    session.add_all([jane, john, laptop])
    link(session, jane, laptop, RelType.OWNS)

    with pytest.raises(IntegrityError):
        link(session, john, laptop, RelType.OWNS)


def test_user_can_own_multiple_devices(session: Session) -> None:
    jane, laptop, phone = User(name="Jane"), Device(name="laptop"), Device(name="phone")
    session.add_all([jane, laptop, phone])
    link(session, jane, laptop, RelType.OWNS)
    link(session, jane, phone, RelType.OWNS)

    assert count(session, "relationships") == 2


# --- deletes ---


def make_jane_graph(session: Session) -> User:
    """Jane owns a laptop, uses Slack, has an email identifier and one source record."""
    jane, laptop, slack = User(name="Jane"), Device(name="laptop"), App(name="Slack")
    run = IngestionRun(filename="okta.json")
    session.add_all([jane, laptop, slack, run])
    session.flush()
    link(session, jane, laptop, RelType.OWNS)
    link(session, jane, slack, RelType.USES)
    session.add_all(
        [
            CIIdentifier(ci=jane, kind=IdentifierKind.EMAIL, value="jane@example.com"),
            SourceRecord(ingestion_id=run.id, ci=jane, record_index=0, raw={"name": "Jane"}),
        ]
    )
    session.commit()
    return jane


def assert_jane_deleted_but_history_kept(session: Session) -> None:
    assert count(session, "users") == 0
    assert count(session, "relationships") == 0
    assert count(session, "ci_identifiers") == 0
    # The raw record survives, detached from the deleted CI.
    record = session.scalars(select(SourceRecord)).one()
    assert record.ci_id is None
    assert record.raw == {"name": "Jane"}
    # The other end of each relationship and the ingestion run are untouched.
    assert session.scalars(select(ConfigurationItem.name).order_by("name")).all() == [
        "Slack",
        "laptop",
    ]
    assert count(session, "ingestion_runs") == 1


def test_database_delete_of_ci_keeps_source_records(session: Session) -> None:
    jane = make_jane_graph(session)

    # Raw SQL, so the database's ON DELETE rules do the work (not the ORM).
    session.execute(text("DELETE FROM configuration_items WHERE id = :id"), {"id": jane.id})
    session.commit()
    session.expire_all()

    assert_jane_deleted_but_history_kept(session)


def test_orm_delete_of_ci_keeps_source_records(session: Session) -> None:
    jane = make_jane_graph(session)

    session.delete(jane)
    session.commit()

    assert_jane_deleted_but_history_kept(session)
