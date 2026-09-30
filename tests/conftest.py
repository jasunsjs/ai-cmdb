from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from cmdb.config import Settings
from cmdb.db import create_db_engine, init_db
from cmdb.ingest.pipeline import IngestAI, ingest_file
from cmdb.main import create_app
from fakes import SAMPLE_FILES, fake_ingest_ai, sample_bytes


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    # _env_file=None keeps tests independent of the developer's real .env.
    return Settings(
        _env_file=None,
        openrouter_api_key=None,
        database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
    )


@pytest.fixture
def engine(settings: Settings) -> Iterator[Engine]:
    engine = create_db_engine(settings.database_url)
    init_db(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine) as session:
        yield session


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    """The app with no AI configured."""
    # Entering the context manager runs the app's lifespan (engine + schema setup).
    with TestClient(create_app(settings, ingest_ai=None, ask_ai=None)) as client:
        yield client


@pytest.fixture
def ingest_ai() -> IngestAI:
    return fake_ingest_ai()


@pytest.fixture
def ingested(session: Session, ingest_ai: IngestAI) -> Session:
    """A session over a database with all sample files ingested (fake AI)."""
    for filename in SAMPLE_FILES:
        ingest_file(session, ingest_ai, filename=filename, content=sample_bytes(filename))
    return session
