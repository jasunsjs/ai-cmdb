from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from cmdb.config import Settings
from cmdb.db import create_db_engine, init_db
from cmdb.main import create_app


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    # _env_file=None keeps tests independent of the developer's real .env.
    return Settings(
        _env_file=None,
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
    # Entering the context manager runs the app's lifespan (engine + schema setup).
    with TestClient(create_app(settings)) as client:
        yield client
