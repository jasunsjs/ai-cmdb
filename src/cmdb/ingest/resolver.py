"""Entity resolution: deciding whether an incoming record is an existing CI or a new one.

Rules decide first: strong identifiers, the device-owner context, and exact names for records
that carry no identifiers. Only genuinely ambiguous cases (same name with different emails,
abbreviated names, near-identical app names) go to the EntityJudge (the LLM). A wrong merge
is worse than a wrong split, so anything uncertain stays separate and is flagged.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session

from cmdb.ingest.canonical import NormalizedRecord, jsonable_value
from cmdb.ingest.errors import AIStepError
from cmdb.ingest.report import RunNotes
from cmdb.models import App, CIIdentifier, CIType, ConfigurationItem, IdentifierKind, User

# When identifiers point at different CIs, trust the strongest kind.
_PRIORITY = [
    IdentifierKind.EXTERNAL_ID,
    IdentifierKind.EMPLOYEE_ID,
    IdentifierKind.EMAIL,
    IdentifierKind.SERIAL,
    IdentifierKind.ASSET_TAG,
    IdentifierKind.MAC,
    IdentifierKind.HOSTNAME,
    IdentifierKind.ALIAS,
]
USER_FUZZY_THRESHOLD = 85
APP_FUZZY_THRESHOLD = 90
MAX_CANDIDATES = 5


def name_key(name: str) -> str:
    """Lowercase, punctuation removed, whitespace collapsed: "John D." -> "john d"."""
    return " ".join(re.sub(r"[^\w\s]", " ", name.lower()).split())


def abbreviation_compatible(a: str, b: str) -> bool:
    """Whether two name keys could be the same name, one abbreviated: "john d" ~ "john doe"."""
    a_tokens, b_tokens = a.split(), b.split()
    if len(a_tokens) < 2 or len(a_tokens) != len(b_tokens):
        return False
    for x, y in zip(a_tokens, b_tokens, strict=True):
        if x == y:
            continue
        short, long = sorted((x, y), key=len)
        if not (len(short) == 1 and long.startswith(short)):
            return False
    return True


def names_compatible(a: str, b: str) -> bool:
    return a == b or abbreviation_compatible(a, b)


@dataclass(frozen=True)
class EntitySummary:
    """What the judge sees about one entity."""

    id: int | None
    ci_type: CIType
    name: str | None
    identifiers: list[tuple[str, str]]
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MatchDecision:
    candidate_id: int | None
    confidence: float
    reason: str


class EntityJudge(Protocol):
    def judge(
        self, incoming: EntitySummary, candidates: list[EntitySummary], *, context: str
    ) -> MatchDecision: ...


@dataclass(frozen=True)
class Resolution:
    ci: ConfigurationItem | None  # None means "create a new CI"
    method: str  # identifier | memo | context | name_exact | llm | new
    confidence: float


_NEW = Resolution(None, "new", 1.0)


class Resolver:
    def __init__(
        self,
        session: Session,
        *,
        judge: EntityJudge | None,
        threshold: float,
        source_system: str | None,
        filename: str,
        notes: RunNotes,
    ) -> None:
        self.session = session
        self.judge = judge
        self.threshold = threshold
        self.source_system = source_system
        self.filename = filename
        self.notes = notes
        self._names: dict[CIType, dict[int, str]] = {}  # per type: ci id -> name key
        self._memo: dict[tuple[CIType, str], tuple[int, float]] = {}

    # --- public ---

    def resolve(self, record: NormalizedRecord, *, context_owner: User | None = None) -> Resolution:
        if (ci := self._by_identifiers(record)) is not None:
            return Resolution(ci, "identifier", 1.0)
        if record.ci_type is CIType.DEVICE:
            return _NEW  # devices are matched by identifiers only

        name = record.values.get("name")
        if not name:
            return _NEW
        key = name_key(name)

        if not record.identifiers and (memo := self._memo.get((record.ci_type, key))):
            ci_id, confidence = memo
            return Resolution(self.session.get(ConfigurationItem, ci_id), "memo", confidence)

        if (
            context_owner is not None
            and names_compatible(key, name_key(context_owner.name))
            and not self._vetoed(record, context_owner.id)
        ):
            return Resolution(context_owner, "context", 0.95)

        candidates = [
            c for c in self._candidates(record.ci_type, key) if not self._vetoed(record, c)
        ]
        if not candidates:
            return _NEW

        exact = [c for c in candidates if self._names[record.ci_type][c] == key]
        if record.ci_type is CIType.USER and not record.identifiers and len(exact) == 1:
            return Resolution(self.session.get(ConfigurationItem, exact[0]), "name_exact", 0.9)

        return self._ask_judge(record, candidates)

    def remember(self, ci: ConfigurationItem, record: NormalizedRecord, confidence: float) -> None:
        """Index a created or merged CI so later records in this run can match it."""
        self._load_names(ci.ci_type)[ci.id] = name_key(ci.name)
        name = record.values.get("name")
        if name and not record.identifiers:
            self._memo[(ci.ci_type, name_key(name))] = (ci.id, confidence)

    # --- steps ---

    def _by_identifiers(self, record: NormalizedRecord) -> ConfigurationItem | None:
        found: dict[int, IdentifierKind] = {}
        for kind, value in record.identifiers:
            source_system = self._scope(kind)
            if source_system is None:
                continue
            ci_id = self.session.scalar(
                select(CIIdentifier.ci_id)
                .join(ConfigurationItem, ConfigurationItem.id == CIIdentifier.ci_id)
                .where(
                    CIIdentifier.kind == kind,
                    CIIdentifier.source_system == source_system,
                    CIIdentifier.value == value,
                    ConfigurationItem.ci_type == record.ci_type,
                )
            )
            if ci_id is not None and (
                ci_id not in found or _PRIORITY.index(kind) < _PRIORITY.index(found[ci_id])
            ):
                found[ci_id] = kind
        if not found:
            return None
        best = min(found, key=lambda ci_id: _PRIORITY.index(found[ci_id]))
        if len(found) > 1:
            name = record.values.get("name") or "record"
            self.notes.warn(
                f"{record.ci_type.value} {name!r}: identifiers point at different CIs "
                f"{sorted(found)}; matched CI {best} by {found[best].value}"
            )
        return self.session.get(ConfigurationItem, best)

    def _candidates(self, ci_type: CIType, key: str) -> list[int]:
        scored: list[tuple[float, int]] = []
        for ci_id, other in self._load_names(ci_type).items():
            if ci_type is CIType.USER:
                if other == key or abbreviation_compatible(key, other):
                    score = 100.0
                else:
                    score = fuzz.token_sort_ratio(key, other)
                threshold = USER_FUZZY_THRESHOLD
            else:
                score = 100.0 if other == key else fuzz.token_set_ratio(key, other)
                threshold = APP_FUZZY_THRESHOLD
            if score >= threshold:
                scored.append((score, ci_id))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [ci_id for _, ci_id in scored[:MAX_CANDIDATES]]

    def _vetoed(self, record: NormalizedRecord, ci_id: int) -> bool:
        """Hard rules that forbid a merge, whatever the names say."""
        incoming: dict[tuple[IdentifierKind, str], set[str]] = {}
        for kind, value in record.identifiers:
            if kind not in (IdentifierKind.EMPLOYEE_ID, IdentifierKind.EXTERNAL_ID):
                continue
            source_system = self._scope(kind)
            if source_system is not None:
                incoming.setdefault((kind, source_system), set()).add(value)
        if not incoming:
            return False
        existing: dict[tuple[IdentifierKind, str], set[str]] = {}
        for kind, source_system, value in self.session.execute(
            select(CIIdentifier.kind, CIIdentifier.source_system, CIIdentifier.value).where(
                CIIdentifier.ci_id == ci_id
            )
        ):
            existing.setdefault((kind, source_system), set()).add(value)
        # Both sides have an employee ID (or an ID from the same system) and they differ.
        return any(
            key in existing and not values & existing[key] for key, values in incoming.items()
        )

    def _ask_judge(self, record: NormalizedRecord, candidate_ids: list[int]) -> Resolution:
        candidates = [self.session.get(ConfigurationItem, ci_id) for ci_id in candidate_ids]
        candidate_names = ", ".join(repr(c.name) for c in candidates)
        incoming_name = record.values.get("name")
        label = f"{record.ci_type.value} {incoming_name!r}"
        if self.judge is None:
            self.notes.warn(f"{label}: possible duplicate of {candidate_names} (AI unavailable)")
            return _NEW
        try:
            decision = self.judge.judge(
                _summarize_record(record),
                [_summarize_ci(self.session, c) for c in candidates],
                context=f"Incoming record from file {self.filename!r}",
            )
        except AIStepError as exc:
            self.notes.warn(f"{label}: possible duplicate of {candidate_names} ({exc})")
            return _NEW

        chosen = next((c for c in candidates if c.id == decision.candidate_id), None)
        accepted = chosen is not None and decision.confidence >= self.threshold
        self.notes.decision(
            type="entity_match",
            ci_type=record.ci_type.value,
            incoming=incoming_name,
            candidates=[{"id": c.id, "name": c.name} for c in candidates],
            matched_id=chosen.id if chosen else None,
            matched=chosen.name if chosen else None,
            confidence=decision.confidence,
            accepted=accepted,
            reason=decision.reason,
        )
        if accepted:
            return Resolution(chosen, "llm", decision.confidence)
        if chosen is not None:
            self.notes.warn(
                f"{label}: possible duplicate of {chosen.name!r} "
                f"(confidence {decision.confidence:.2f} below threshold)"
            )
        return _NEW

    # --- helpers ---

    def _scope(self, kind: IdentifierKind) -> str | None:
        """The source_system an identifier is stored under; None if it can't be scoped."""
        if kind is IdentifierKind.EXTERNAL_ID:
            return self.source_system
        return ""

    def _load_names(self, ci_type: CIType) -> dict[int, str]:
        if ci_type not in self._names:
            rows = self.session.execute(
                select(ConfigurationItem.id, ConfigurationItem.name).where(
                    ConfigurationItem.ci_type == ci_type
                )
            )
            self._names[ci_type] = {ci_id: name_key(name) for ci_id, name in rows}
        return self._names[ci_type]


def _summarize_record(record: NormalizedRecord) -> EntitySummary:
    details = {k: v for k, v in record.values.items() if k != "name"}
    return EntitySummary(
        id=None,
        ci_type=record.ci_type,
        name=record.values.get("name"),
        identifiers=[(kind.value, value) for kind, value in record.identifiers],
        details=_jsonable(details),
    )


def _summarize_ci(session: Session, ci: ConfigurationItem) -> EntitySummary:
    identifiers = session.execute(
        select(CIIdentifier.kind, CIIdentifier.value).where(CIIdentifier.ci_id == ci.id)
    )
    details: dict[str, Any] = {"status": ci.status}
    if isinstance(ci, User):
        details.update(
            email=ci.email, department=ci.department, team=ci.team, groups=list(ci.groups)
        )
    elif isinstance(ci, App):
        details.update(
            vendor=ci.vendor, category=ci.category, app_type=ci.app_type, is_stub=ci.is_stub
        )
    return EntitySummary(
        id=ci.id,
        ci_type=ci.ci_type,
        name=ci.name,
        identifiers=[(kind.value, value) for kind, value in identifiers],
        details=_jsonable({k: v for k, v in details.items() if v not in (None, [])}),
    )


def _jsonable(values: dict[str, Any]) -> dict[str, Any]:
    return {key: jsonable_value(value) for key, value in values.items()}
