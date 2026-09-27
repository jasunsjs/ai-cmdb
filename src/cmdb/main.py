"""FastAPI application factory and server entry point."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from sqlalchemy.orm import sessionmaker

from cmdb.config import Settings, get_settings
from cmdb.db import create_db_engine, init_db


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_db_engine(settings.database_url)
        init_db(engine)
        app.state.engine = engine
        app.state.session_factory = sessionmaker(engine, expire_on_commit=False)
        yield
        engine.dispose()

    app = FastAPI(
        title="AI CMDB",
        description="Ingest IT/SaaS inventory data into normalized configuration items "
        "and query them in natural language.",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = settings

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()


def run() -> None:
    """Entry point for `uv run cmdb-serve`."""
    settings = get_settings()
    uvicorn.run(
        "cmdb.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
    )
