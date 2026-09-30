"""FastAPI application factory and server entry point."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import sessionmaker

from cmdb.ai import build_ask_ai, build_ingest_ai
from cmdb.ai.client import build_llm_client
from cmdb.api import ask, cis, ingest, ingestions
from cmdb.ask.service import AskAI, AskError
from cmdb.config import Settings, get_settings
from cmdb.db import create_db_engine, init_db
from cmdb.ingest.errors import IngestError
from cmdb.ingest.pipeline import IngestAI

_FROM_SETTINGS: Any = object()  # sentinel: build the AI services from settings


def create_app(
    settings: Settings | None = None,
    *,
    ingest_ai: IngestAI | None = _FROM_SETTINGS,
    ask_ai: AskAI | None = _FROM_SETTINGS,
) -> FastAPI:
    """Build the app. Pass `ingest_ai` / `ask_ai` (or None) to override the LLM services."""
    settings = settings or get_settings()
    if ingest_ai is _FROM_SETTINGS or ask_ai is _FROM_SETTINGS:
        llm = build_llm_client(settings)
        if ingest_ai is _FROM_SETTINGS:
            ingest_ai = build_ingest_ai(llm)
        if ask_ai is _FROM_SETTINGS:
            ask_ai = build_ask_ai(llm)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_db_engine(settings.database_url)
        try:
            init_db(engine)
            app.state.engine = engine
            app.state.session_factory = sessionmaker(engine, expire_on_commit=False)
            yield
        finally:
            engine.dispose()

    app = FastAPI(
        title="AI CMDB",
        description="Ingest IT/SaaS inventory data into normalized configuration items "
        "and query them in natural language.",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.ingest_ai = ingest_ai
    app.state.ask_ai = ask_ai

    @app.exception_handler(IngestError)
    async def ingest_error(_request: Request, exc: IngestError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.message, "ingestion_id": exc.ingestion_id},
        )

    @app.exception_handler(AskError)
    async def ask_error(_request: Request, exc: AskError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code, content={"detail": exc.message, "sql": exc.sql}
        )

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, Any]:
        return {"status": "ok", "llm_configured": ingest_ai is not None}

    app.include_router(ingest.router)
    app.include_router(ask.router)
    app.include_router(cis.router)
    app.include_router(ingestions.router)
    return app


def run() -> None:
    """Entry point for `uv run cmdb-serve`.

    The app is built by uvicorn at startup (factory=True) rather than at import time, so
    importing this module (e.g. in tests) never reads `.env`.
    Equivalent: `uv run uvicorn cmdb.main:create_app --factory`.
    """
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(), format="%(levelname)s %(name)s: %(message)s"
    )
    uvicorn.run(
        "cmdb.main:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
    )
