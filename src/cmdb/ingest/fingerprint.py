"""Structural fingerprints: the cache key for schema mappings.

Two record sets share a fingerprint when they have the same record path and the same set of
(flattened) keys. Values, key order and file format don't matter, so every export with the
same structure reuses one mapping.
"""

import hashlib
from typing import Any

from cmdb.ingest.records import flatten


def key_paths(records: list[dict[str, Any]]) -> list[str]:
    """Sorted union of flattened keys across all records (sparse records are covered)."""
    paths: set[str] = set()
    for record in records:
        paths.update(flatten(record))
    return sorted(paths)


def fingerprint(record_path: str, records: list[dict[str, Any]]) -> str:
    payload = "\n".join([record_path, *key_paths(records)])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
