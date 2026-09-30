"""Running LLM-written SQL safely.

Defense in depth, enforced by SQLite itself rather than by inspecting SQL text:
- the connection is opened read-only (mode=ro) with PRAGMA query_only;
- an authorizer allows only SELECT/recursive CTEs, ordinary functions, and reads of the
  CMDB views (or of the tables *through* those views), json_each and json_tree;
- sqlite3 refuses to run more than one statement at a time;
- a progress handler interrupts queries that run too long, and rows are capped.
"""

import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cmdb.ask.schema import ALLOWED_VIEWS

_TABLE_FUNCTIONS = {"json_each", "json_tree"}
# The tables the views are built from. SQLite reports some reads for views with LEFT JOINs as
# a read of the whole table (empty column name) that isn't attributed to the view; those
# reads return no column data, so they are allowed for these tables only.
_VIEW_BASE_TABLES = {"configuration_items", "devices", "users", "apps", "relationships"}
_LEADING_COMMENTS = re.compile(r"^(?:\s+|--[^\n]*(?:\n|$)|/\*.*?\*/)*", re.DOTALL)


class UnsafeQueryError(ValueError):
    """The query tried to do something other than read the CMDB views."""


class QueryError(ValueError):
    """The query is not valid SQL for these views, or it ran too long."""


@dataclass(frozen=True)
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool


def open_readonly(database_path: str) -> sqlite3.Connection:
    uri = Path(database_path).resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, check_same_thread=False)
    connection.execute("PRAGMA query_only = ON")
    return connection


def run_query(
    connection: sqlite3.Connection, sql: str, *, max_rows: int, timeout_s: float
) -> QueryResult:
    statement = clean_sql(sql)
    if not re.match(r"(select|with)\b", statement, re.IGNORECASE):
        raise UnsafeQueryError("Only SELECT queries are allowed")

    deadline = time.monotonic() + timeout_s
    connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    connection.set_authorizer(_authorize)
    try:
        cursor = connection.execute(statement)
        rows = cursor.fetchmany(max_rows + 1)
        columns = [description[0] for description in cursor.description or []]
    except sqlite3.ProgrammingError as exc:  # e.g. more than one statement
        raise UnsafeQueryError(str(exc)) from exc
    except sqlite3.DatabaseError as exc:
        message = str(exc)
        if "not authorized" in message or "prohibited" in message:
            raise UnsafeQueryError(
                "The query may only read these views: " + ", ".join(ALLOWED_VIEWS)
            ) from exc
        if "interrupted" in message:
            raise QueryError(f"The query took longer than {timeout_s:g}s") from exc
        raise QueryError(message) from exc
    finally:
        connection.set_authorizer(None)
        connection.set_progress_handler(None, 0)

    return QueryResult(
        columns=columns,
        rows=[list(row) for row in rows[:max_rows]],
        truncated=len(rows) > max_rows,
    )


def clean_sql(sql: str) -> str:
    """Strip leading comments, surrounding whitespace and trailing semicolons."""
    return _LEADING_COMMENTS.sub("", sql).strip().rstrip(";").strip()


def _authorize(action: int, arg1: str | None, arg2: str | None, db: str | None, source: Any) -> int:
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_FUNCTION:
        return (
            sqlite3.SQLITE_DENY if (arg2 or "").lower() == "load_extension" else sqlite3.SQLITE_OK
        )
    if action == sqlite3.SQLITE_READ and (
        arg1 in ALLOWED_VIEWS
        or source in ALLOWED_VIEWS
        or arg1 in _TABLE_FUNCTIONS
        or (arg2 == "" and arg1 in _VIEW_BASE_TABLES)
        or db is None  # a CTE defined by the query itself; real tables report "main"
    ):
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY
