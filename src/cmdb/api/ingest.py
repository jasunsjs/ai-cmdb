"""POST /ingest: upload a raw inventory file."""

import re
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from cmdb.api.deps import app_settings, get_ingest_ai, get_session
from cmdb.config import Settings
from cmdb.ingest.pipeline import IngestAI, ingest_file
from cmdb.ingest.report import IngestReport

router = APIRouter(tags=["ingest"])

_SOURCE_SYSTEM = re.compile(r"[A-Za-z0-9 _-]{1,64}")


@router.post("/ingest", response_model=IngestReport)
def ingest(
    file: Annotated[UploadFile, File(description="A JSON, YAML or CSV inventory export")],
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(app_settings)],
    ai: Annotated[IngestAI | None, Depends(get_ingest_ai)],
    source_system: Annotated[
        str | None,
        Form(description="The system that produced the file (e.g. okta); scopes external IDs"),
    ] = None,
) -> IngestReport:
    if source_system is not None and not _SOURCE_SYSTEM.fullmatch(source_system):
        raise HTTPException(422, "source_system may only contain letters, digits, spaces, _ or -")
    content = file.file.read(settings.max_upload_bytes + 1)
    if len(content) > settings.max_upload_bytes:
        raise HTTPException(413, f"File is larger than {settings.max_upload_bytes} bytes")
    return ingest_file(
        session,
        ai,
        filename=file.filename or "upload",
        content=content,
        source_system=source_system,
        match_threshold=settings.entity_match_auto_merge_threshold,
    )
