"""POST /ask: question -> SQL (LLM) -> guarded execution -> answer (LLM).

The SQL is always returned alongside the answer so every answer can be checked.
"""

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel

from cmdb.ask.schema import describe_examples, describe_schema
from cmdb.ask.sql_guard import QueryError, QueryResult, UnsafeQueryError, open_readonly, run_query
from cmdb.errors import AIStepError

logger = logging.getLogger(__name__)

NO_ROWS_ANSWER = "No matching records found."


class SqlPlan(BaseModel):
    sql: str | None = None
    clarification: str | None = None


class SqlGenerator(Protocol):
    def generate(
        self,
        question: str,
        *,
        schema: str,
        examples: str,
        previous_sql: str | None = None,
        error: str | None = None,
    ) -> SqlPlan: ...


class AnswerWriter(Protocol):
    def write(self, question: str, *, sql: str, result: QueryResult) -> str: ...


@dataclass(frozen=True)
class AskAI:
    sql_generator: SqlGenerator
    answer_writer: AnswerWriter


class AskResponse(BaseModel):
    question: str
    answer: str
    sql: str | None
    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool


class AskError(Exception):
    def __init__(self, status_code: int, message: str, sql: str | None = None) -> None:
        self.status_code = status_code
        self.message = message
        self.sql = sql
        super().__init__(message)


def ask(
    question: str, *, database_path: str, ai: AskAI, max_rows: int, timeout_s: float
) -> AskResponse:
    connection = open_readonly(database_path)
    try:
        schema = describe_schema(connection)
        examples = describe_examples()
        sql: str | None = None
        error: str | None = None
        for attempt in (1, 2):
            try:
                plan = ai.sql_generator.generate(
                    question, schema=schema, examples=examples, previous_sql=sql, error=error
                )
            except AIStepError as exc:
                raise AskError(502, f"Could not generate SQL: {exc}", sql) from exc
            if not plan.sql:
                answer = plan.clarification or "I can't answer that from the CMDB."
                return AskResponse(
                    question=question,
                    answer=answer,
                    sql=None,
                    columns=[],
                    rows=[],
                    row_count=0,
                    truncated=False,
                )
            sql = plan.sql
            try:
                result = run_query(connection, sql, max_rows=max_rows, timeout_s=timeout_s)
                break
            except (QueryError, UnsafeQueryError) as exc:
                error = str(exc)
                logger.info("ask attempt %d failed: %s", attempt, error)
                if attempt == 2:
                    raise AskError(422, f"Could not answer the question: {error}", sql) from exc
    finally:
        connection.close()

    if result.rows:
        try:
            answer = ai.answer_writer.write(question, sql=sql, result=result)
        except AIStepError as exc:
            raise AskError(502, f"Could not write the answer: {exc}", sql) from exc
    else:
        answer = NO_ROWS_ANSWER

    return AskResponse(
        question=question,
        answer=answer,
        sql=sql,
        columns=result.columns,
        rows=[dict(zip(result.columns, row, strict=True)) for row in result.rows],
        row_count=len(result.rows),
        truncated=result.truncated,
    )
