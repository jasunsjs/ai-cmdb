"""Resolving values the rules couldn't classify, in one batched call per run.

The classifier (the LLM) only *translates* a messy value into a form the rules accept; its
answer is passed back through the field's own normalizer, so it can never write a value the
schema doesn't allow.
"""

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Protocol

from cmdb.ingest.canonical import CANONICAL_FIELDS, NormalizedRecord, set_field
from cmdb.ingest.errors import AIStepError, UnrecognizedValueError
from cmdb.ingest.normalizers import (
    normalize_app_type,
    normalize_bool,
    normalize_datetime,
    normalize_os,
    normalize_status,
)
from cmdb.ingest.report import RunNotes
from cmdb.models import CIType


@dataclass(frozen=True)
class Classifiable:
    expected: str  # the answer format the classifier must use
    normalizer: Callable[[Any], Any]


_BOOL = Classifiable("true or false", normalize_bool)
_DATETIME = Classifiable("an ISO 8601 timestamp, e.g. 2024-07-22T10:00:00Z", normalize_datetime)

# Fields worth asking about. Invalid IPs, emails or MACs can't be recovered by guessing.
CLASSIFIABLE_FIELDS: dict[str, Classifiable] = {
    "status": Classifiable("one of: active, inactive, suspended, unknown", normalize_status),
    "app_type": Classifiable(
        "saas (run by a vendor) or on_prem (operated by us, wherever hosted)", normalize_app_type
    ),
    "encryption_enabled": _BOOL,
    "mfa_enabled": _BOOL,
    "sso_enabled": _BOOL,
    "os": Classifiable(
        "the operating system as '<name> <version>', e.g. 'macOS 14.5' or 'Windows 11'",
        normalize_os,
    ),
    "last_seen": _DATETIME,
    "last_login": _DATETIME,
}


@dataclass(frozen=True)
class ClassificationItem:
    id: int
    ci_type: CIType
    field: str
    description: str
    expected: str
    raw: Any


class ValueClassifier(Protocol):
    def classify(self, items: list[ClassificationItem]) -> dict[int, Any]:
        """Answers by item id (a string/bool in the expected format, or None if unsure)."""
        ...


def resolve_unrecognized(
    records: list[NormalizedRecord], classifier: ValueClassifier | None, notes: RunNotes
) -> None:
    """Classify the unrecognized values of these records (and their related records)."""
    groups: dict[tuple[CIType, str, str], list[NormalizedRecord]] = {}
    raw_values: dict[tuple[CIType, str, str], Any] = {}
    for record in _walk(records):
        for field_name, raw in record.unrecognized:
            if field_name not in CLASSIFIABLE_FIELDS:
                continue
            key = (record.ci_type, field_name, json.dumps(raw, sort_keys=True, default=str))
            groups.setdefault(key, []).append(record)
            raw_values[key] = raw
    if not groups or classifier is None:
        return

    keys = list(groups)
    items = [
        ClassificationItem(
            id=index,
            ci_type=ci_type,
            field=field_name,
            description=_description(ci_type, field_name),
            expected=CLASSIFIABLE_FIELDS[field_name].expected,
            raw=raw_values[(ci_type, field_name, raw_key)],
        )
        for index, (ci_type, field_name, raw_key) in enumerate(keys)
    ]
    try:
        answers = classifier.classify(items)
    except AIStepError as exc:
        notes.warn(f"Value classification unavailable: {exc}")
        return

    for item in items:
        value = _normalize_answer(item.field, answers.get(item.id))
        notes.decision(
            type="value_classification",
            ci_type=item.ci_type.value,
            field=item.field,
            raw=item.raw,
            answer=answers.get(item.id),
            accepted=value is not None,
        )
        if value is None:
            continue
        for record in groups[keys[item.id]]:
            set_field(record, item.field, value)


def _normalize_answer(field_name: str, answer: Any) -> Any:
    if answer is None:
        return None
    try:
        value = CLASSIFIABLE_FIELDS[field_name].normalizer(answer)
    except UnrecognizedValueError:
        return None
    if field_name == "os" and value == (None, None):
        return None
    return value


def _description(ci_type: CIType, field_name: str) -> str:
    if field_name == "os":
        return "Operating system"
    return CANONICAL_FIELDS[ci_type][field_name].description


def _walk(records: list[NormalizedRecord]) -> Iterator[NormalizedRecord]:
    for record in records:
        yield record
        for related in record.relations.values():
            yield from _walk(related)
