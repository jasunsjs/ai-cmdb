"""Ingestion runs, raw source records (provenance), and cached schema mappings."""

from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cmdb.models.base import Base, str_enum, utcnow
from cmdb.models.ci import ConfigurationItem
from cmdb.models.enums import CIType, IngestionStatus, MappingSource


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(512))
    detected_format: Mapped[str | None] = mapped_column(String(16))
    fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[IngestionStatus] = mapped_column(
        str_enum(IngestionStatus), default=IngestionStatus.PENDING
    )
    stats: Mapped[dict[str, Any]] = mapped_column(default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None]

    source_records: Mapped[list["SourceRecord"]] = relationship(
        back_populates="ingestion", cascade="all, delete-orphan", passive_deletes=True
    )


class SourceRecord(Base):
    """One raw input record from one ingestion run, kept for audit and replay.

    `ci_id` is the record's primary CI (e.g. the device for a hardware row). Secondary CIs
    built from the same record (e.g. the device owner) reference it via field_provenance.

    Records are permanent history: deleting a CI only detaches them (ci_id -> NULL), so they
    remain available for audit and for rebuilding CIs (e.g. after undoing a bad merge).
    """

    __tablename__ = "source_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    ingestion_id: Mapped[int] = mapped_column(
        ForeignKey("ingestion_runs.id", ondelete="CASCADE"), index=True
    )
    ci_id: Mapped[int | None] = mapped_column(
        ForeignKey("configuration_items.id", ondelete="SET NULL"), index=True
    )
    record_index: Mapped[int]
    raw: Mapped[dict[str, Any]]
    normalized: Mapped[dict[str, Any] | None]
    observed_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    ingestion: Mapped[IngestionRun] = relationship(back_populates="source_records")
    ci: Mapped[ConfigurationItem | None] = relationship(back_populates="source_records")


class SchemaMapping(Base):
    """Cached source-field -> CI-field mapping for one input shape, keyed by fingerprint."""

    __tablename__ = "schema_mappings"

    fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    ci_type: Mapped[CIType] = mapped_column(str_enum(CIType))
    record_path: Mapped[str] = mapped_column(String(512), default="")
    spec: Mapped[dict[str, Any]]
    created_by: Mapped[MappingSource] = mapped_column(str_enum(MappingSource))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
