"""Read endpoints for configuration items."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from cmdb.api.deps import get_session
from cmdb.api.schemas import (
    CIDetail,
    CISummary,
    IdentifierOut,
    RelationshipOut,
    SourceRecordOut,
)
from cmdb.ingest.canonical import jsonable_value
from cmdb.models import CIStatus, CIType, ConfigurationItem, SourceRecord

router = APIRouter(prefix="/cis", tags=["cis"])


@router.get("", response_model=list[CISummary])
def list_cis(
    session: Annotated[Session, Depends(get_session)],
    type: Annotated[CIType | None, Query(description="device, user or app")] = None,
    status: CIStatus | None = None,
    q: Annotated[str | None, Query(description="Case-insensitive name filter")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ConfigurationItem]:
    query = select(ConfigurationItem).order_by(ConfigurationItem.id).limit(limit).offset(offset)
    if type is not None:
        query = query.where(ConfigurationItem.ci_type == type)
    if status is not None:
        query = query.where(ConfigurationItem.status == status)
    if q:
        query = query.where(ConfigurationItem.name.ilike(f"%{q}%"))
    return list(session.scalars(query))


@router.get("/{ci_id}", response_model=CIDetail)
def get_ci(ci_id: int, session: Annotated[Session, Depends(get_session)]) -> CIDetail:
    ci = session.get(ConfigurationItem, ci_id)
    if ci is None:
        raise HTTPException(404, f"CI {ci_id} not found")

    type_columns = [c.key for c in inspect(type(ci)).local_table.columns if c.key != "ci_id"]
    relationships = [
        RelationshipOut(
            id=edge.id,
            direction="outgoing",
            rel_type=edge.rel_type,
            other_id=edge.target.id,
            other_type=edge.target.ci_type,
            other_name=edge.target.name,
            confidence=edge.confidence,
            ingestion_id=edge.ingestion_id,
            created_at=edge.created_at,
        )
        for edge in ci.outgoing
    ] + [
        RelationshipOut(
            id=edge.id,
            direction="incoming",
            rel_type=edge.rel_type,
            other_id=edge.source.id,
            other_type=edge.source.ci_type,
            other_name=edge.source.name,
            confidence=edge.confidence,
            ingestion_id=edge.ingestion_id,
            created_at=edge.created_at,
        )
        for edge in ci.incoming
    ]
    source_records = session.scalars(
        select(SourceRecord).where(SourceRecord.ci_id == ci.id).order_by(SourceRecord.id)
    )
    return CIDetail(
        id=ci.id,
        ci_type=ci.ci_type,
        name=ci.name,
        status=ci.status,
        created_at=ci.created_at,
        updated_at=ci.updated_at,
        fields={key: jsonable_value(getattr(ci, key)) for key in type_columns},
        attributes=dict(ci.attributes),
        field_provenance=dict(ci.field_provenance),
        identifiers=[
            IdentifierOut(kind=i.kind, source_system=i.source_system, value=i.value)
            for i in ci.identifiers
        ],
        relationships=relationships,
        source_records=[
            SourceRecordOut(
                id=r.id,
                ingestion_id=r.ingestion_id,
                record_index=r.record_index,
                observed_at=r.observed_at,
            )
            for r in source_records
        ],
    )
