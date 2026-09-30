"""Database engine setup, schema initialization, and the session dependency."""

from collections.abc import Iterator
from importlib.resources import files
from pathlib import Path
from typing import Any

from fastapi import Request
from sqlalchemy import Engine, create_engine, event, make_url
from sqlalchemy.orm import Session

from cmdb.models import Base


def create_db_engine(database_url: str) -> Engine:
    url = make_url(database_url)
    connect_args: dict[str, Any] = {}

    is_sqlite = url.get_backend_name() == "sqlite"
    if is_sqlite:
        # FastAPI runs sync endpoints in a thread pool, so connections cross threads.
        connect_args["check_same_thread"] = False
        # SQLite allows one writer at a time; wait for the lock instead of failing at once.
        connect_args["timeout"] = 30
        if url.database and url.database != ":memory:":
            Path(url.database).parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(url, connect_args=connect_args)
    if is_sqlite:
        event.listen(engine, "connect", _set_sqlite_pragmas)
    return engine


def _set_sqlite_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
    cursor = dbapi_connection.cursor()
    # SQLite ignores foreign keys (and therefore ON DELETE CASCADE) unless enabled per connection.
    cursor.execute("PRAGMA foreign_keys=ON")
    # WAL lets readers (e.g. /ask) run while an ingestion is writing.
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


def init_db(engine: Engine) -> None:
    """Create tables and (re)create views. Safe to run on every startup."""
    Base.metadata.create_all(engine)

    views_sql = files("cmdb").joinpath("sql", "views.sql").read_text(encoding="utf-8")
    raw_connection = engine.raw_connection()
    try:
        raw_connection.driver_connection.executescript(views_sql)
    finally:
        raw_connection.close()


def get_session(request: Request) -> Iterator[Session]:
    """FastAPI dependency yielding a session bound to the app's engine."""
    with request.app.state.session_factory() as session:
        yield session
