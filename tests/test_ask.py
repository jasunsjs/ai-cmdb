from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import make_url
from sqlalchemy.orm import Session

from cmdb.ask.service import NO_ROWS_ANSWER, AskAI, AskError, SqlPlan, ask
from cmdb.ask.sql_guard import QueryResult
from cmdb.config import Settings
from cmdb.errors import AIStepError
from cmdb.main import create_app

MFA_SQL = "SELECT name, email FROM v_users WHERE mfa_enabled = 0 ORDER BY name"


class ScriptedSql:
    """Returns scripted plans in order; records what it was asked."""

    def __init__(self, *plans: SqlPlan | Exception) -> None:
        self.plans = list(plans)
        self.calls: list[dict[str, Any]] = []

    def generate(self, question: str, **kwargs: Any) -> SqlPlan:
        self.calls.append({"question": question, **kwargs})
        plan = self.plans.pop(0)
        if isinstance(plan, Exception):
            raise plan
        return plan


class CountingWriter:
    def __init__(self) -> None:
        self.calls = 0

    def write(self, question: str, *, sql: str, result: QueryResult) -> str:
        self.calls += 1
        return f"{len(result.rows)} rows"


def run(settings: Settings, generator: ScriptedSql, writer: CountingWriter | None = None) -> Any:
    ai = AskAI(sql_generator=generator, answer_writer=writer or CountingWriter())
    return ask(
        "Which users don't have MFA?",
        database_path=make_url(settings.database_url).database,
        ai=ai,
        max_rows=200,
        timeout_s=5,
    )


def test_question_is_answered_from_the_data(settings: Settings, ingested: Session) -> None:
    generator = ScriptedSql(SqlPlan(sql=MFA_SQL))

    response = run(settings, generator)

    assert [row["name"] for row in response.rows] == ["Alex Johnson", "Carlos S.", "Michael Brown"]
    assert response.sql == MFA_SQL
    assert response.answer == "3 rows"
    assert response.columns == ["name", "email"]
    assert "v_users" in generator.calls[0]["schema"]
    assert "mfa_enabled (1 = yes, 0 = no, NULL = unknown)" in generator.calls[0]["schema"]


def test_a_failing_query_is_repaired_once(settings: Settings, ingested: Session) -> None:
    generator = ScriptedSql(SqlPlan(sql="SELECT nme FROM v_users"), SqlPlan(sql=MFA_SQL))

    response = run(settings, generator)

    assert response.row_count == 3
    assert generator.calls[1]["previous_sql"] == "SELECT nme FROM v_users"
    assert "no such column" in generator.calls[1]["error"]


def test_unsafe_sql_twice_is_refused(settings: Settings, ingested: Session) -> None:
    generator = ScriptedSql(SqlPlan(sql="DELETE FROM users"), SqlPlan(sql="DROP VIEW v_users"))

    with pytest.raises(AskError) as caught:
        run(settings, generator)

    assert caught.value.status_code == 422
    assert caught.value.sql == "DROP VIEW v_users"


def test_clarifications_are_passed_through(settings: Settings, ingested: Session) -> None:
    generator = ScriptedSql(SqlPlan(clarification="The CMDB has no weather data."))

    response = run(settings, generator)

    assert (response.answer, response.sql, response.rows) == (
        "The CMDB has no weather data.",
        None,
        [],
    )


def test_no_rows_skips_the_answer_writer(settings: Settings, ingested: Session) -> None:
    writer = CountingWriter()
    generator = ScriptedSql(SqlPlan(sql="SELECT name FROM v_users WHERE name = 'Nobody'"))

    response = run(settings, generator, writer)

    assert response.answer == NO_ROWS_ANSWER
    assert writer.calls == 0


def test_ai_failure_is_a_502(settings: Settings, ingested: Session) -> None:
    with pytest.raises(AskError) as caught:
        run(settings, ScriptedSql(AIStepError("down")))

    assert caught.value.status_code == 502


# --- API ---


@pytest.fixture
def ask_client(settings: Settings, ingested: Session) -> Iterator[TestClient]:
    ai = AskAI(sql_generator=ScriptedSql(SqlPlan(sql=MFA_SQL)), answer_writer=CountingWriter())
    with TestClient(create_app(settings, ingest_ai=None, ask_ai=ai)) as client:
        yield client


def test_ask_endpoint(ask_client: TestClient) -> None:
    response = ask_client.post("/ask", json={"question": "Which users don't have MFA?"})

    assert response.status_code == 200
    body = response.json()
    assert body["row_count"] == 3
    assert body["sql"] == MFA_SQL
    assert body["rows"][0] == {"name": "Alex Johnson", "email": "a.johnson.contractor@example.com"}


@pytest.mark.parametrize("question", ["", "x" * 501])
def test_ask_validates_the_question(ask_client: TestClient, question: str) -> None:
    assert ask_client.post("/ask", json={"question": question}).status_code == 422


def test_ask_without_ai_is_a_503(client: TestClient) -> None:
    response = client.post("/ask", json={"question": "Which users don't have MFA?"})

    assert response.status_code == 503
