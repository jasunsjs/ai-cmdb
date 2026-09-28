"""Parsed document -> records: locating the record list, file metadata, key flattening."""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from cmdb.ingest.errors import ParseError
from cmdb.ingest.parsers import Format, decode, detect_format, parse_document

# How deep to search nested mappings for the record list (e.g. `inventory.devices` is 2).
MAX_SEARCH_DEPTH = 5


@dataclass(frozen=True)
class RecordSet:
    record_path: str  # dotted path to the record list; "" when the document is the list
    records: list[dict[str, Any]]  # raw, unflattened records
    metadata: dict[str, Any]  # the rest of the document, flattened (export dates, summaries)


@dataclass(frozen=True)
class ParsedFile:
    format: Format
    record_path: str
    records: list[dict[str, Any]]
    metadata: dict[str, Any]


def load_file(filename: str | None, content: bytes) -> ParsedFile:
    """Decode, detect the format, parse, and locate the records of an uploaded file."""
    text = decode(content)
    fmt = detect_format(filename, text)
    record_set = find_records(parse_document(text, fmt))
    return ParsedFile(
        format=fmt,
        record_path=record_set.record_path,
        records=record_set.records,
        metadata=record_set.metadata,
    )


def find_records(document: Any) -> RecordSet:
    """Find the list of record dicts in a document.

    A root-level list is the record list. In a mapping, the largest list of dicts found in
    nested mappings wins (ties go to the shallowest). Only one record list per file is used.
    """
    if isinstance(document, list):
        _require_records(document, where="the document root")
        return RecordSet(record_path="", records=document, metadata={})

    if isinstance(document, dict):
        candidates = list(_record_lists(document, prefix="", depth=1))
        if not candidates:
            raise ParseError("No list of records found in the document")
        path, records = max(candidates, key=lambda c: (len(c[1]), -c[0].count(".")))
        metadata = {k: v for k, v in flatten(document).items() if k != path}
        return RecordSet(record_path=path, records=records, metadata=metadata)

    raise ParseError("Document must be a list of records or a mapping that contains one")


def flatten(record: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flatten nested mappings into dotted keys. Lists are kept as values.

    {"network": {"primary_ip": "10.0.0.1"}, "apps": ["Slack"]}
        -> {"network.primary_ip": "10.0.0.1", "apps": ["Slack"]}
    """
    flat: dict[str, Any] = {}
    for key, value in record.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict) and value:
            flat.update(flatten(value, path))
        else:
            flat[path] = value
    return flat


def _record_lists(
    mapping: dict[str, Any], prefix: str, depth: int
) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    for key, value in mapping.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
            yield path, value
        elif isinstance(value, dict) and depth < MAX_SEARCH_DEPTH:
            yield from _record_lists(value, path, depth + 1)


def _require_records(items: list[Any], where: str) -> None:
    if not items:
        raise ParseError(f"No records found at {where}")
    if not all(isinstance(item, dict) for item in items):
        raise ParseError(f"Every item at {where} must be an object (a record)")
