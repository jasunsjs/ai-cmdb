"""FastAPI application factory and server entry point."""

import uvicorn
from fastapi import FastAPI

from cmdb.config import get_settings


def create_app() -> FastAPI:
    app = FastAPI(
        title="AI CMDB",
        description="Ingest IT/SaaS inventory data into normalized configuration items "
        "and query them in natural language.",
        version="0.1.0",
    )

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
