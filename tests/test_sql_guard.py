import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from cmdb.ask.schema import EXAMPLES, describe_schema
from cmdb.ask.sql_guard import (
    QueryError,
    UnsafeQueryError,
    clean_sql,
    open_readonly,
    run_query,
)
from cmdb.db import create_db_engine, init_db
from cmdb.ingest.pipeline import ingest_file
from fakes import SAMPLE_FILES, fake_ingest_ai, sample_bytes


@pytest.fixture(scope="module")
def database(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The sample data ingested once for the whole module (these tests only read)."""
    path = tmp_path_factory.mktemp("guard") / "cmdb.db"
    engine = create_db_engine(f"sqlite:///{path.as_posix()}")
    init_db(engine)
    with Session(engine) as session:
        ai = fake_ingest_ai()
        for filename in SAMPLE_FILES:
            ingest_file(session, ai, filename=filename, content=sample_bytes(filename))
    engine.dispose()
    return path


@pytest.fixture
def ro(database: Path) -> Iterator[sqlite3.Connection]:
    connection = open_readonly(str(database))
    yield connection
    connection.close()


def query(connection: sqlite3.Connection, sql: str, **kwargs: float) -> list[list]:
    options = {"max_rows": 200, "timeout_s": 5.0, **kwargs}
    return run_query(connection, sql, **options).rows


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT name FROM v_users WHERE mfa_enabled = 0",
        "SELECT hostname, owner_name FROM v_devices WHERE status = 'inactive'",
        "SELECT * FROM v_apps",
        "SELECT * FROM v_user_apps",
        "SELECT * FROM v_app_integrations",
        "SELECT * FROM v_relationships",
        "WITH u AS (SELECT name FROM v_users) SELECT count(*) FROM u",
        "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n WHERE x < 5) "
        "SELECT count(*) FROM n",
        "SELECT u.name FROM v_users AS u, json_each(u.groups) AS g WHERE g.value = 'Admins'",
        "SELECT lower(name), upper(email) FROM v_users",
        "  -- a comment\n/* another */ select name from v_apps;",
    ],
)
def test_reads_of_the_views_are_allowed(ro: sqlite3.Connection, sql: str) -> None:
    assert query(ro, sql)


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM users",
        "UPDATE configuration_items SET name = 'x'",
        "INSERT INTO ci_identifiers (ci_id, kind, source_system, value) "
        "VALUES (1, 'alias', '', 'x')",
        "DROP VIEW v_users",
        "CREATE TABLE t (x)",
        "ATTACH DATABASE 'other.db' AS other",
        "PRAGMA table_info(users)",
        "SELECT 1; DELETE FROM users",
        "WITH x AS (SELECT 1) DELETE FROM users",
        "SELECT * FROM source_records",
        "SELECT email FROM users",
        "SELECT name FROM sqlite_master",
        "SELECT load_extension('evil')",
    ],
)
def test_anything_else_is_rejected(ro: sqlite3.Connection, sql: str) -> None:
    with pytest.raises(UnsafeQueryError):
        query(ro, sql)


def test_attributes_are_readable_through_the_views(ro: sqlite3.Connection) -> None:
    cost_example = dict(EXAMPLES)["Which apps cost the most per year?"]

    rows = query(ro, cost_example)

    assert rows[0] == ["Salesforce CRM", 45000.0]
    assert query(
        ro,
        "SELECT name FROM v_apps "
        "WHERE json_extract(attributes, '$.\"server_location\"') LIKE '%AWS%' ORDER BY name",
    ) == [["Jenkins CI/CD"], ["PostgreSQL Database"]]


def test_attributes_are_not_readable_from_the_base_table(ro: sqlite3.Connection) -> None:
    with pytest.raises(UnsafeQueryError):
        query(ro, "SELECT attributes FROM configuration_items")


def test_schema_description_lists_attribute_keys(ro: sqlite3.Connection) -> None:
    schema = describe_schema(ro)
    apps_section = schema.split("v_apps:")[1].split("v_user_apps:")[0]

    assert "  - attributes (JSON object" in apps_section
    assert "    - annual_cost_usd: 18000" in apps_section
    assert "    - server_location:" in apps_section


def test_invalid_sql_is_a_query_error(ro: sqlite3.Connection) -> None:
    with pytest.raises(QueryError, match="no such column"):
        query(ro, "SELECT shoe_size FROM v_users")


def test_runaway_queries_are_interrupted(ro: sqlite3.Connection) -> None:
    forever = "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n) SELECT max(x) FROM n"

    with pytest.raises(QueryError, match="took longer"):
        query(ro, forever, timeout_s=0.2)


def test_rows_are_capped(ro: sqlite3.Connection) -> None:
    result = run_query(ro, "SELECT name FROM v_apps", max_rows=2, timeout_s=5)

    assert len(result.rows) == 2
    assert result.truncated is True
    assert result.columns == ["name"]


def test_the_connection_itself_is_read_only(ro: sqlite3.Connection) -> None:
    # Even without the authorizer (not installed outside run_query), writes fail.
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("DELETE FROM users")


def test_clean_sql() -> None:
    assert clean_sql("  -- note\n SELECT 1 ;; ") == "SELECT 1"
