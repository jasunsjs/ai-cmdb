"""The ingestion report returned by POST /ingest and stored on the ingestion run."""

from enum import IntEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from cmdb.models import CIType, IngestionStatus


class CIOutcome(IntEnum):
    """What happened to a CI in a run; a CI touched several times keeps the strongest."""

    UNCHANGED = 0
    UPDATED = 1
    CREATED = 2


class CICounts(BaseModel):
    created: int = 0
    updated: int = 0
    unchanged: int = 0


class IngestReport(BaseModel):
    ingestion_id: int
    status: IngestionStatus
    filename: str
    format: str | None = None
    fingerprint: str | None = None
    ci_type: CIType | None = None
    mapping_source: Literal["cache", "llm", "bundled"] | None = None
    source_system: str | None = None
    records: int = 0
    cis: dict[str, CICounts] = Field(default_factory=dict)
    relationships_created: dict[str, int] = Field(default_factory=dict)
    ai_decisions: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class RunNotes:
    """Collects what a run did: CI outcomes, new edges, AI decisions and warnings."""

    def __init__(self) -> None:
        self.outcomes: dict[int, tuple[CIType, CIOutcome]] = {}
        self.relationships_created: dict[str, int] = {}
        self.ai_decisions: list[dict[str, Any]] = []
        self.warnings: list[str] = []

    def outcome(self, ci_id: int, ci_type: CIType, outcome: CIOutcome) -> None:
        previous = self.outcomes.get(ci_id)
        if previous is None or outcome > previous[1]:
            self.outcomes[ci_id] = (ci_type, outcome)

    def relationship(self, rel_type: str) -> None:
        self.relationships_created[rel_type] = self.relationships_created.get(rel_type, 0) + 1

    def decision(self, **decision: Any) -> None:
        self.ai_decisions.append(decision)

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    def ci_counts(self) -> dict[str, CICounts]:
        counts: dict[str, CICounts] = {}
        for ci_type, outcome in self.outcomes.values():
            bucket = counts.setdefault(ci_type.value, CICounts())
            name = outcome.name.lower()
            setattr(bucket, name, getattr(bucket, name) + 1)
        return counts
