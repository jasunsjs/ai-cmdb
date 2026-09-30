"""POST /ask: natural-language questions about the CMDB."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import make_url

from cmdb.api.deps import app_settings, get_ask_ai
from cmdb.ask.service import AskAI, AskResponse, ask
from cmdb.config import Settings

router = APIRouter(tags=["ask"])


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500, examples=["Which users don't have MFA?"])


@router.post("/ask", response_model=AskResponse)
def ask_question(
    body: AskRequest,
    settings: Annotated[Settings, Depends(app_settings)],
    ai: Annotated[AskAI | None, Depends(get_ask_ai)],
) -> AskResponse:
    if ai is None:
        raise HTTPException(503, "Natural-language queries need an LLM; set OPENROUTER_API_KEY")
    url = make_url(settings.database_url)
    if url.get_backend_name() != "sqlite" or not url.database:
        raise HTTPException(501, "/ask currently supports SQLite databases only")
    return ask(
        body.question.strip(),
        database_path=url.database,
        ai=ai,
        max_rows=settings.ask_max_rows,
        timeout_s=settings.ask_timeout_seconds,
    )
