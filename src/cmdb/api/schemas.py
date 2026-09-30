"""Response models for the read endpoints."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

from cmdb.models import CIStatus, CIType, IngestionStatus, MappingSource


class CISummary(BaseModel):
    id: int
    ci_type: CIType
    name: str
    status: CIStatus
    updated_at: datetime


class IdentifierOut(BaseModel):
    kind: str
    source_system: str
    value: str


class RelationshipOut(BaseModel):
    id: int
    direction: Literal["outgoing", "incoming"]
    rel_type: str
    other_id: int
    other_type: CIType
    other_name: str
    confidence: float
    ingestion_id: int | None
    created_at: datetime


class SourceRecordOut(BaseModel):
    id: int
    ingestion_id: int
    record_index: int
    observed_at: datetime | None


class CIDetail(CISummary):
    created_at: datetime
    fields: dict[str, Any]
    attributes: dict[str, Any]
    field_provenance: dict[str, Any]
    identifiers: list[IdentifierOut]
    relationships: list[RelationshipOut]
    source_records: list[SourceRecordOut]


class IngestionOut(BaseModel):
    id: int
    filename: str
    detected_format: str | None
    fingerprint: str | None
    status: IngestionStatus
    error: str | None
    created_at: datetime
    finished_at: datetime | None
    report: dict[str, Any]


class MappingOut(BaseModel):
    fingerprint: str
    ci_type: CIType
    record_path: str
    spec: dict[str, Any]
    created_by: MappingSource
    created_at: datetime
    updated_at: datetime
