"""The ingestion pipeline behind POST /ingest.

parse -> fingerprint -> get mapping -> apply mapping -> normalize -> classify leftovers
      -> resolve -> merge/write -> report

Every run is recorded in `ingestion_runs`, including failed ones. All CI changes from a run
are written in one transaction, so a failure never leaves a half-ingested file behind.
"""

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from cmdb.ingest.bundled import bundled_mapping
from cmdb.ingest.canonical import (
    CANONICAL_RELATIONS,
    Direction,
    NormalizedRecord,
    normalize_record,
    to_jsonable,
)
from cmdb.ingest.classify import ValueClassifier, resolve_unrecognized
from cmdb.ingest.errors import (
    AIStepError,
    IngestError,
    MappingGenerationError,
    ParseError,
    UnrecognizedValueError,
)
from cmdb.ingest.fingerprint import fingerprint, key_paths
from cmdb.ingest.mapping import (
    MappingGenerator,
    MappingSpec,
    apply_mapping,
    build_context,
    get_cached_mapping,
    save_mapping,
    validate_mapping,
)
from cmdb.ingest.merge import (
    Observation,
    attach_identifiers,
    create_ci,
    current_owner,
    link,
    merge_ci,
)
from cmdb.ingest.normalizers import normalize_datetime
from cmdb.ingest.records import ParsedFile, load_file
from cmdb.ingest.report import CIOutcome, IngestReport, RunNotes
from cmdb.ingest.resolver import EntityJudge, Resolver
from cmdb.models import (
    CIType,
    ConfigurationItem,
    Device,
    IngestionRun,
    IngestionStatus,
    MappingSource,
    SourceRecord,
    utcnow,
)

logger = logging.getLogger(__name__)

# Metadata keys that hold the export time of a whole file (the fallback observation time).
_EXPORT_DATE_KEY = re.compile(
    r"(export(ed)?_(date|at|time)|generated_(at|date)|as_of|snapshot_(date|at))$"
)


@dataclass(frozen=True)
class IngestAI:
    """The AI-assisted steps. Classification and matching are optional refinements."""

    mapper: MappingGenerator
    classifier: ValueClassifier | None = None
    judge: EntityJudge | None = None


@dataclass
class _Prepared:
    index: int
    raw: dict[str, Any]
    extras: dict[str, Any]
    record: NormalizedRecord
    observed_at: datetime | None


def ingest_file(
    session: Session,
    ai: IngestAI | None,
    *,
    filename: str,
    content: bytes,
    source_system: str | None = None,
    match_threshold: float = 0.8,
) -> IngestReport:
    started = time.perf_counter()
    run = IngestionRun(filename=filename)
    session.add(run)
    session.commit()

    try:
        parsed = load_file(filename, content)
    except ParseError as exc:
        _fail(session, run, str(exc))
        raise IngestError(422, str(exc), run.id) from exc

    paths = key_paths(parsed.records)
    run.detected_format = parsed.format.value
    run.fingerprint = fingerprint(parsed.record_path, parsed.records)
    session.commit()

    notes = RunNotes()
    spec, mapping_source = _get_mapping(session, ai, run, parsed, paths, filename, notes)
    effective_source_system = _slug(source_system) or spec.source_system

    try:
        prepared = [
            _prepare(index, raw, spec, parsed.metadata, notes)
            for index, raw in enumerate(parsed.records)
        ]
        resolve_unrecognized([p.record for p in prepared], ai.classifier if ai else None, notes)
        writer = _Writer(
            session,
            notes=notes,
            ingestion_id=run.id,
            source_system=effective_source_system,
            resolver=Resolver(
                session,
                judge=ai.judge if ai else None,
                threshold=match_threshold,
                source_system=effective_source_system,
                filename=filename,
                notes=notes,
            ),
        )
        for item in prepared:
            writer.write(item)

        report = IngestReport(
            ingestion_id=run.id,
            status=IngestionStatus.COMPLETED,
            filename=filename,
            format=parsed.format.value,
            fingerprint=run.fingerprint,
            ci_type=spec.ci_type,
            mapping_source=mapping_source,
            source_system=effective_source_system,
            records=len(parsed.records),
            cis=notes.ci_counts(),
            relationships_created=notes.relationships_created,
            ai_decisions=notes.ai_decisions,
            warnings=notes.warnings,
        )
        run.status = IngestionStatus.COMPLETED
        run.stats = report.model_dump(mode="json")
        run.finished_at = utcnow()
        session.commit()
    except Exception as exc:
        session.rollback()
        _fail(session, run, f"{type(exc).__name__}: {exc}")
        logger.exception("ingestion %s failed", run.id)
        raise IngestError(500, f"Ingestion failed: {exc}", run.id) from exc

    logger.info(
        "ingestion id=%s file=%s records=%d mapping=%s ai_decisions=%d warnings=%d duration_ms=%d",
        run.id,
        filename,
        len(parsed.records),
        mapping_source,
        len(notes.ai_decisions),
        len(notes.warnings),
        (time.perf_counter() - started) * 1000,
    )
    return report


def _get_mapping(
    session: Session,
    ai: IngestAI | None,
    run: IngestionRun,
    parsed: ParsedFile,
    paths: list[str],
    filename: str,
    notes: RunNotes,
) -> tuple[MappingSpec, str]:
    assert run.fingerprint is not None
    spec = get_cached_mapping(session, run.fingerprint)
    if spec is not None:
        if errors := validate_mapping(spec, paths):
            message = "The stored mapping for this format is invalid: " + "; ".join(errors)
            _fail(session, run, message)
            raise IngestError(422, message, run.id)
        return spec, "cache"

    fallback = bundled_mapping(run.fingerprint)
    if ai is None:
        if fallback is not None:
            notes.warn("No LLM is configured; used the bundled mapping for this known format")
            return fallback, "bundled"
        message = "This file format has not been seen before and no LLM is configured to map it"
        _fail(session, run, message)
        raise IngestError(503, message, run.id)

    context = build_context(filename, parsed.record_path, parsed.records, parsed.metadata)
    try:
        spec = ai.mapper.generate(context)
        if errors := validate_mapping(spec, paths):
            raise MappingGenerationError("; ".join(errors))
    except AIStepError as exc:
        if fallback is not None:
            notes.warn(f"LLM mapping failed ({exc}); used the bundled mapping for this format")
            notes.decision(type="schema_mapping", fingerprint=run.fingerprint, error=str(exc))
            return fallback, "bundled"
        _fail(session, run, f"Mapping generation failed: {exc}")
        raise IngestError(502, f"Could not map this file format: {exc}", run.id) from exc

    spec = save_mapping(
        session,
        fingerprint=run.fingerprint,
        record_path=parsed.record_path,
        spec=spec,
        created_by=MappingSource.LLM,
    )
    session.commit()  # keep the mapping even if the rest of the run fails
    notes.decision(
        type="schema_mapping",
        fingerprint=run.fingerprint,
        ci_type=spec.ci_type.value,
        fields=spec.fields,
        related=[r.model_dump() for r in spec.related],
        observed_at=spec.observed_at,
        source_system=spec.source_system,
    )
    return spec, "llm"


def _prepare(
    index: int, raw: dict[str, Any], spec: MappingSpec, metadata: dict[str, Any], notes: RunNotes
) -> _Prepared:
    mapped = apply_mapping(spec, raw)
    record = normalize_record(spec.ci_type, mapped.values)
    observed_at = _observed_at(mapped.observed_at_raw, metadata, notes)
    return _Prepared(index, raw, mapped.extras, record, observed_at)


def _observed_at(raw: Any, metadata: dict[str, Any], notes: RunNotes) -> datetime | None:
    """The record's own observation time, else the file's export time, else undated."""
    for candidate in (raw, _export_date(metadata)):
        try:
            if (value := normalize_datetime(candidate)) is not None:
                return value
        except UnrecognizedValueError:
            notes.warn(f"Could not read the observation time {candidate!r}")
    return None


def _export_date(metadata: dict[str, Any]) -> Any:
    for key, value in metadata.items():
        if _EXPORT_DATE_KEY.search(key.rsplit(".", 1)[-1].lower()):
            return value
    return None


class _Writer:
    """Writes prepared records: source record, primary CI, related CIs and edges."""

    def __init__(
        self,
        session: Session,
        *,
        notes: RunNotes,
        ingestion_id: int,
        source_system: str | None,
        resolver: Resolver,
    ) -> None:
        self.session = session
        self.notes = notes
        self.ingestion_id = ingestion_id
        self.source_system = source_system
        self.resolver = resolver

    def write(self, item: _Prepared) -> None:
        source = SourceRecord(
            ingestion_id=self.ingestion_id,
            record_index=item.index,
            raw=item.raw,
            normalized=to_jsonable(item.record),
            observed_at=item.observed_at,
        )
        self.session.add(source)
        self.session.flush()
        obs = Observation(item.observed_at, source.id)

        primary, _ = self._upsert(item.record, item.extras, obs, full_record=True)
        source.ci_id = primary.id

        relations = CANONICAL_RELATIONS[item.record.ci_type]
        for name, related_records in item.record.relations.items():
            relation = relations[name]
            # A name in a list ("GitHub" in someone's apps) only references the entity; it
            # says nothing about when the entity looked like that, so it may only fill blanks.
            # An embedded entity (an owner block) is a real observation of that entity.
            related_obs = Observation(None, source.id) if relation.many else obs
            for related in related_records:
                context_owner = (
                    current_owner(self.session, primary) if isinstance(primary, Device) else None
                )
                other, confidence = self._upsert(
                    related, {}, related_obs, full_record=False, context_owner=context_owner
                )
                if relation.direction is Direction.OUTGOING:
                    edge_source, edge_target = primary, other
                else:
                    edge_source, edge_target = other, primary
                link(
                    self.session,
                    edge_source,
                    edge_target,
                    relation.rel_type,
                    confidence=confidence,
                    ingestion_id=self.ingestion_id,
                    obs=obs,
                    notes=self.notes,
                )

    def _upsert(
        self,
        record: NormalizedRecord,
        extras: dict[str, Any],
        obs: Observation,
        *,
        full_record: bool,
        context_owner: Any = None,
    ) -> tuple[ConfigurationItem, float]:
        if record.unrecognized:
            extras = dict(extras)
            label = f"{record.ci_type.value} {record.values.get('name')!r}"
            for field_name, raw in record.unrecognized:
                extras[field_name] = raw
                self.notes.warn(
                    f"{label}: could not interpret {field_name}={raw!r}; kept in attributes"
                )

        resolution = self.resolver.resolve(record, context_owner=context_owner)
        if resolution.ci is None:
            stub = record.ci_type is CIType.APP and not full_record
            ci = create_ci(self.session, record, extras, obs, stub=stub)
            outcome = CIOutcome.CREATED
        else:
            ci = resolution.ci
            changed = merge_ci(ci, record, extras, obs, full_record=full_record)
            outcome = CIOutcome.UPDATED if changed else CIOutcome.UNCHANGED

        added = attach_identifiers(
            self.session, ci, record, source_system=self.source_system, notes=self.notes
        )
        if added and outcome is CIOutcome.UNCHANGED:
            outcome = CIOutcome.UPDATED
        self.session.flush()
        self.notes.outcome(ci.id, ci.ci_type, outcome)
        self.resolver.remember(ci, record, resolution.confidence)
        return ci, resolution.confidence


def _fail(session: Session, run: IngestionRun, message: str) -> None:
    run.status = IngestionStatus.FAILED
    run.error = message
    run.finished_at = utcnow()
    session.commit()


def _slug(value: str | None) -> str | None:
    if value is None:
        return None
    slug = re.sub(r"\s+", "_", value.strip().lower())
    return slug or None
