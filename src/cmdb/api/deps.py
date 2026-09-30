"""FastAPI dependencies: the database session, settings and AI services on app.state."""

from typing import Any

from fastapi import Request

from cmdb.config import Settings
from cmdb.db import get_session

__all__ = ["app_settings", "get_ask_ai", "get_ingest_ai", "get_session"]


def app_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_ingest_ai(request: Request) -> Any:
    return request.app.state.ingest_ai


def get_ask_ai(request: Request) -> Any:
    return request.app.state.ask_ai
