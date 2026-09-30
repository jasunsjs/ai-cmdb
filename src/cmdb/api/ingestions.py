"""Read endpoints for ingestion runs and cached schema mappings."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from cmdb.api.deps import get_session
from cmdb.api.schemas import IngestionOut, MappingOut
from cmdb.models import IngestionRun, SchemaMapping

router = APIRouter(tags=["ingestions"])


def _ingestion_out(run: IngestionRun) -> IngestionOut:
    return IngestionOut(
        id=run.id,
        filename=run.filename,
        detected_format=run.detected_format,
        fingerprint=run.fingerprint,
        status=run.status,
        error=run.error,
        created_at=run.created_at,
        finished_at=run.finished_at,
        report=dict(run.stats),
    )


@router.get("/ingestions", response_model=list[IngestionOut])
def list_ingestions(
    session: Annotated[Session, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[IngestionOut]:
    runs = session.scalars(select(IngestionRun).order_by(IngestionRun.id.desc()).limit(limit))
    return [_ingestion_out(run) for run in runs]


@router.get("/ingestions/{ingestion_id}", response_model=IngestionOut)
def get_ingestion(
    ingestion_id: int, session: Annotated[Session, Depends(get_session)]
) -> IngestionOut:
    run = session.get(IngestionRun, ingestion_id)
    if run is None:
        raise HTTPException(404, f"Ingestion {ingestion_id} not found")
    return _ingestion_out(run)


@router.get("/mappings", response_model=list[MappingOut])
def list_mappings(session: Annotated[Session, Depends(get_session)]) -> list[SchemaMapping]:
    return list(session.scalars(select(SchemaMapping).order_by(SchemaMapping.created_at)))


@router.get("/mappings/{fingerprint}", response_model=MappingOut)
def get_mapping(
    fingerprint: str, session: Annotated[Session, Depends(get_session)]
) -> SchemaMapping:
    mapping = session.get(SchemaMapping, fingerprint)
    if mapping is None:
        raise HTTPException(404, f"Mapping {fingerprint} not found")
    return mapping
