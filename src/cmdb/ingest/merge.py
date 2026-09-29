"""Writing normalized records into CIs: field-level merge, identifiers and relationships.

Precedence per field (tracked in `field_provenance`):
- a dated observation beats an undated one;
- between dated observations the newer wins (ties go to the incoming value);
- an undated observation only fills empty fields;
- a user's name keeps the more complete of two compatible names ("John Doe" over "John D.").
These rules make the result independent of the order files are uploaded in.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from cmdb.ingest.canonical import NormalizedRecord
from cmdb.ingest.report import RunNotes
from cmdb.ingest.resolver import abbreviation_compatible, name_key
from cmdb.models import (
    App,
    CIIdentifier,
    CIRelationship,
    CIStatus,
    CIType,
    ConfigurationItem,
    Device,
    IdentifierKind,
    RelType,
    User,
)

_CI_CLASSES: dict[CIType, type[ConfigurationItem]] = {
    CIType.DEVICE: Device,
    CIType.USER: User,
    CIType.APP: App,
}


@dataclass(frozen=True)
class Observation:
    """When a value was observed and which source record it came from."""

    observed_at: datetime | None  # None = undated
    source_record_id: int

    def provenance(self) -> dict[str, Any]:
        return {
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "source_record_id": self.source_record_id,
        }


def takes_precedence(provenance: dict[str, Any] | None, current: Any, obs: Observation) -> bool:
    """Whether an incoming observation should replace the current value."""
    if current is None:
        return True
    if obs.observed_at is None:
        return False  # undated values only fill blanks
    existing = (provenance or {}).get("observed_at")
    if existing is None:
        return True  # dated beats undated
    return obs.observed_at >= datetime.fromisoformat(existing)


def create_ci(
    session: Session,
    record: NormalizedRecord,
    extras: dict[str, Any],
    obs: Observation,
    *,
    stub: bool = False,
) -> ConfigurationItem:
    ci = _CI_CLASSES[record.ci_type](
        name=_fallback_name(record),
        status=CIStatus.UNKNOWN,
        attributes={},
        field_provenance={},
    )
    if isinstance(ci, App):
        ci.is_stub = stub
    for field_name, value in record.values.items():
        _assign(ci, field_name, value, obs)
    for key, value in extras.items():
        ci.attributes[key] = value
        ci.field_provenance[f"attributes.{key}"] = obs.provenance()
    session.add(ci)
    session.flush()
    return ci


def merge_ci(
    ci: ConfigurationItem,
    record: NormalizedRecord,
    extras: dict[str, Any],
    obs: Observation,
    *,
    full_record: bool,
) -> bool:
    """Merge an observation into an existing CI; returns whether anything changed.

    `full_record` is True for a file's primary records and False for related records that
    only name the CI (e.g. an app in a user's app list).
    """
    changed = False
    for field_name, value in record.values.items():
        current = getattr(ci, field_name)
        if value == current:
            continue
        if field_name == "name" and isinstance(ci, User):
            if abbreviation_compatible(name_key(value), name_key(current)):
                # Keep the more complete of two compatible names, whatever their dates.
                if len(value) > len(current):
                    _assign(ci, field_name, value, obs)
                    changed = True
                continue
        # A field with no provenance was never observed (e.g. the default status "unknown"),
        # so it counts as blank.
        provenance = ci.field_provenance.get(field_name)
        if takes_precedence(provenance, current if provenance else None, obs):
            _assign(ci, field_name, value, obs)
            changed = True

    for key, value in extras.items():
        current = ci.attributes.get(key)
        if value == current or value is None:
            continue
        if takes_precedence(ci.field_provenance.get(f"attributes.{key}"), current, obs):
            ci.attributes[key] = value
            ci.field_provenance[f"attributes.{key}"] = obs.provenance()
            changed = True

    if full_record and isinstance(ci, App) and ci.is_stub:
        ci.is_stub = False
        changed = True
    return changed


def attach_identifiers(
    session: Session,
    ci: ConfigurationItem,
    record: NormalizedRecord,
    *,
    source_system: str | None,
    notes: RunNotes,
) -> bool:
    """Add the record's identifiers to the CI; returns whether any were new."""
    added = False
    for kind, value in record.identifiers:
        scope = source_system if kind is IdentifierKind.EXTERNAL_ID else ""
        if scope is None:
            notes.warn(
                f"{ci.ci_type.value} {ci.name!r}: external ID {value!r} skipped because the "
                "source system is unknown (pass source_system when uploading)"
            )
            continue
        existing = session.scalar(
            select(CIIdentifier).where(
                CIIdentifier.kind == kind,
                CIIdentifier.source_system == scope,
                CIIdentifier.value == value,
            )
        )
        if existing is not None:
            if existing.ci_id != ci.id:
                notes.warn(
                    f"{ci.ci_type.value} {ci.name!r}: {kind.value} {value!r} already belongs to "
                    f"CI {existing.ci_id}; not moved"
                )
            continue
        session.add(CIIdentifier(ci_id=ci.id, kind=kind, source_system=scope, value=value))
        session.flush()
        added = True
    return added


def current_owner(session: Session, device: ConfigurationItem) -> User | None:
    return session.scalar(
        select(User)
        .join(CIRelationship, CIRelationship.source_ci_id == User.id)
        .where(CIRelationship.target_ci_id == device.id, CIRelationship.rel_type == RelType.OWNS)
    )


def link(
    session: Session,
    source: ConfigurationItem,
    target: ConfigurationItem,
    rel_type: RelType,
    *,
    confidence: float,
    ingestion_id: int,
    obs: Observation,
    notes: RunNotes,
) -> bool:
    """Create the edge if it doesn't exist; returns whether one was created.

    A device has at most one owner: a different incoming owner replaces the current one
    only if its observation takes precedence.
    """
    if source.id == target.id:
        notes.warn(f"{source.ci_type.value} {source.name!r}: ignored a {rel_type} link to itself")
        return False
    existing = session.scalar(
        select(CIRelationship).where(
            CIRelationship.source_ci_id == source.id,
            CIRelationship.target_ci_id == target.id,
            CIRelationship.rel_type == rel_type,
        )
    )
    if existing is not None:
        return False

    if rel_type is RelType.OWNS:
        previous = session.scalar(
            select(CIRelationship).where(
                CIRelationship.target_ci_id == target.id, CIRelationship.rel_type == RelType.OWNS
            )
        )
        if previous is not None:
            if not takes_precedence(target.field_provenance.get("owner"), previous, obs):
                notes.warn(
                    f"device {target.name!r}: kept its current owner; the owner "
                    f"{source.name!r} from this file is from an older or undated observation"
                )
                return False
            session.delete(previous)
            session.flush()
        target.field_provenance["owner"] = obs.provenance()

    session.add(
        CIRelationship(
            source_ci_id=source.id,
            target_ci_id=target.id,
            rel_type=rel_type,
            confidence=round(confidence, 3),
            ingestion_id=ingestion_id,
        )
    )
    session.flush()
    notes.relationship(rel_type.value)
    return True


def _assign(ci: ConfigurationItem, field_name: str, value: Any, obs: Observation) -> None:
    setattr(ci, field_name, value)
    ci.field_provenance[field_name] = obs.provenance()


def _fallback_name(record: NormalizedRecord) -> str:
    """A name for CIs whose source has none (the `name` column is required)."""
    if name := record.values.get("name"):
        return name
    identifiers = dict(record.identifiers)
    if record.ci_type is CIType.DEVICE:
        for kind in (IdentifierKind.ASSET_TAG, IdentifierKind.SERIAL, IdentifierKind.MAC):
            if kind in identifiers:
                return identifiers[kind]
        return "unknown device"
    if record.ci_type is CIType.USER and (email := record.values.get("email")):
        return email.split("@")[0]
    return f"unknown {record.ci_type.value}"
