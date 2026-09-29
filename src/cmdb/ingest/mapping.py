"""Mapping specs: how one source format's fields translate into the canonical vocabulary.

A spec is generated once per structural fingerprint (by the LLM, or written by hand), cached
in `schema_mappings`, and applied deterministically to every record of that shape.
"""

import re
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cmdb.ingest.canonical import CANONICAL_FIELDS, CANONICAL_RELATIONS
from cmdb.ingest.records import flatten
from cmdb.models import CIType, MappingSource, SchemaMapping

_SOURCE_SYSTEM = re.compile(r"[a-z0-9_-]{1,64}")

# At least one of these must be mapped so every record can be named or identified.
_IDENTIFYING_FIELDS = {
    CIType.DEVICE: {"hostname", "name", "serial_number", "asset_tag"},
    CIType.USER: {"name", "email"},
    CIType.APP: {"name"},
}


class RelatedMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relation: str = Field(description="Relation name, e.g. owner, apps, integrations")
    fields: dict[str, str] = Field(
        description="Related CI's canonical field -> source key path. For list relations "
        "(apps, integrations) map only 'name' to the path holding the list of names."
    )


class MappingSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ci_type: CIType = Field(description="The kind of CI each record describes")
    fields: dict[str, str] = Field(description="Canonical field -> source key path")
    related: list[RelatedMapping] = Field(default_factory=list)
    observed_at: str | None = Field(
        default=None,
        description="Source key path of when the source system last observed the record "
        "(e.g. last_checkin), or null",
    )
    source_system: str | None = Field(
        default=None,
        description="Short lowercase slug of the system that produced the file (e.g. okta), "
        "or null if unknown",
    )

    @field_validator("source_system", mode="before")
    @classmethod
    def _slug(cls, value: object) -> object:
        if isinstance(value, str):
            value = re.sub(r"\s+", "_", value.strip().lower())
            return value or None
        return value


@dataclass(frozen=True)
class MappedRecord:
    values: dict[str, Any]  # canonical field / relation name -> raw value
    extras: dict[str, Any]  # flattened source keys the mapping doesn't use
    observed_at_raw: Any


@dataclass(frozen=True)
class MappingContext:
    """What a mapping generator gets to see about a new source format."""

    filename: str
    record_path: str
    key_paths: list[str]
    samples: dict[str, list[Any]]  # key path -> up to 3 distinct example values
    metadata: dict[str, Any]


class MappingGenerator(Protocol):
    def generate(self, context: MappingContext) -> MappingSpec:
        """A mapping valid for `context.key_paths`; raises MappingGenerationError otherwise."""
        ...


def validate_mapping(spec: MappingSpec, key_paths: list[str]) -> list[str]:
    """All problems with a spec for records with these key paths; empty when valid."""
    errors: list[str] = []
    known_paths = set(key_paths)
    fields = CANONICAL_FIELDS[spec.ci_type]
    relations = CANONICAL_RELATIONS[spec.ci_type]

    def check_path(path: str, where: str) -> None:
        if path not in known_paths:
            errors.append(f"{where}: source path {path!r} does not exist in the records")

    for target, path in spec.fields.items():
        if target not in fields:
            errors.append(f"fields: {target!r} is not a {spec.ci_type} field")
        check_path(path, f"fields.{target}")

    if not _IDENTIFYING_FIELDS[spec.ci_type] & set(spec.fields):
        options = ", ".join(sorted(_IDENTIFYING_FIELDS[spec.ci_type]))
        errors.append(f"fields: map at least one identifying field ({options})")

    uses_external_id = "external_id" in spec.fields
    for related in spec.related:
        relation = relations.get(related.relation)
        if relation is None:
            errors.append(f"related: {related.relation!r} is not a {spec.ci_type} relation")
            continue
        if relation.many and set(related.fields) != {"name"}:
            errors.append(f"related.{related.relation}: list relations map exactly 'name'")
        target_fields = CANONICAL_FIELDS[relation.target]
        for target, path in related.fields.items():
            if target not in target_fields:
                errors.append(
                    f"related.{related.relation}: {target!r} is not a {relation.target} field"
                )
            check_path(path, f"related.{related.relation}.{target}")
        uses_external_id |= "external_id" in related.fields

    if spec.observed_at is not None:
        check_path(spec.observed_at, "observed_at")

    if spec.source_system is not None and not _SOURCE_SYSTEM.fullmatch(spec.source_system):
        errors.append("source_system: use a short slug of lowercase letters, digits, _ or -")
    if uses_external_id and spec.source_system is None:
        errors.append("source_system: required when external_id is mapped")
    return errors


def apply_mapping(spec: MappingSpec, record: dict[str, Any]) -> MappedRecord:
    """Rename one raw record's fields into canonical names. Values are not changed."""
    flat = flatten(record)
    used = set(spec.fields.values())
    values: dict[str, Any] = {target: flat.get(path) for target, path in spec.fields.items()}

    relations = CANONICAL_RELATIONS[spec.ci_type]
    for related in spec.related:
        used.update(related.fields.values())
        if relations[related.relation].many:
            values[related.relation] = flat.get(related.fields["name"])
        else:
            values[related.relation] = {f: flat.get(p) for f, p in related.fields.items()}

    if spec.observed_at is not None:
        used.add(spec.observed_at)
    extras = {key: value for key, value in flat.items() if key not in used}
    observed_at_raw = flat.get(spec.observed_at) if spec.observed_at else None
    return MappedRecord(values=values, extras=extras, observed_at_raw=observed_at_raw)


def build_context(
    filename: str, record_path: str, records: list[dict[str, Any]], metadata: dict[str, Any]
) -> MappingContext:
    samples: dict[str, list[Any]] = {}
    for record in records:
        for path, value in flatten(record).items():
            examples = samples.setdefault(path, [])
            if len(examples) < 3 and value not in examples:
                examples.append(value)
    return MappingContext(
        filename=filename,
        record_path=record_path,
        key_paths=sorted(samples),
        samples={path: samples[path] for path in sorted(samples)},
        metadata=metadata,
    )


# --- cache ---


def get_cached_mapping(session: Session, fingerprint: str) -> MappingSpec | None:
    row = session.get(SchemaMapping, fingerprint)
    return MappingSpec.model_validate(row.spec) if row is not None else None


def save_mapping(
    session: Session,
    *,
    fingerprint: str,
    record_path: str,
    spec: MappingSpec,
    created_by: MappingSource,
) -> MappingSpec:
    """Store a mapping; if another upload stored one first, keep and return that one."""
    try:
        with session.begin_nested():
            session.add(
                SchemaMapping(
                    fingerprint=fingerprint,
                    ci_type=spec.ci_type,
                    record_path=record_path,
                    spec=spec.model_dump(mode="json"),
                    created_by=created_by,
                )
            )
    except IntegrityError:
        existing = get_cached_mapping(session, fingerprint)
        if existing is None:
            raise
        return existing
    return spec
