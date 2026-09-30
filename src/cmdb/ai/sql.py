"""LLM text-to-SQL and answer writing for POST /ask."""

import json
from datetime import UTC, datetime

from pydantic import BaseModel

from cmdb.ai.client import LLMClient, LLMError
from cmdb.ask.service import SqlPlan
from cmdb.ask.sql_guard import QueryResult
from cmdb.errors import AIStepError

_MAX_ANSWER_ROWS = 50

SQL_SYSTEM_PROMPT = """\
You translate questions about an IT Configuration Management Database (CMDB) into one
SQLite query.

Rules:
- Write a single read-only SELECT (or WITH ... SELECT) statement in SQLite syntax.
- Use only these views and columns:
{schema}
- Match names and text case-insensitively and loosely, e.g. name LIKE '%slack%'.
- Return the identifying columns people need (names, emails, hostnames), not just ids.
- Today's date (UTC) is {today}.
- If the question can't be answered from these views, return sql = null and a short
  clarification explaining what is available.

Examples:
{examples}
"""

ANSWER_SYSTEM_PROMPT = """\
You answer a question about an IT inventory using only the SQL query results given. Be
concise (one to three sentences, or a short list). Mention counts where useful. Never add
facts that are not in the results. If the results were truncated, say so.
"""


class _AnswerReply(BaseModel):
    answer: str


class LLMSqlGenerator:
    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def generate(
        self,
        question: str,
        *,
        schema: str,
        examples: str,
        previous_sql: str | None = None,
        error: str | None = None,
    ) -> SqlPlan:
        system = (
            SQL_SYSTEM_PROMPT.replace("{schema}", schema)
            .replace("{today}", datetime.now(UTC).date().isoformat())
            .replace("{examples}", examples)
        )
        user = f"Question: {question}"
        if previous_sql:
            user += (
                f"\n\nYour previous query failed.\nQuery: {previous_sql}\nError: {error}\n"
                "Return a corrected query."
            )
        try:
            return self.llm.complete_json(
                system=system, user=user, schema=SqlPlan, purpose="ask_sql"
            )
        except LLMError as exc:
            raise AIStepError(str(exc)) from exc


class LLMAnswerWriter:
    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def write(self, question: str, *, sql: str, result: QueryResult) -> str:
        rows = [
            dict(zip(result.columns, row, strict=True)) for row in result.rows[:_MAX_ANSWER_ROWS]
        ]
        shown = len(rows)
        truncated = result.truncated or len(result.rows) > shown
        user = "\n".join(
            [
                f"Question: {question}",
                f"SQL: {sql}",
                f"Rows ({shown} shown{', results truncated' if truncated else ''}):",
                *(json.dumps(row, default=str) for row in rows),
            ]
        )
        try:
            reply = self.llm.complete_json(
                system=ANSWER_SYSTEM_PROMPT, user=user, schema=_AnswerReply, purpose="ask_answer"
            )
        except LLMError as exc:
            raise AIStepError(str(exc)) from exc
        return reply.answer
