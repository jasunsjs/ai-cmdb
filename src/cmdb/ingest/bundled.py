"""Hand-written mappings shipped with the app, used when the LLM can't provide one.

Each file in `bundled_mappings/` holds the mapping for one known source format, keyed by its
structural fingerprint. The pipeline prefers a cached or LLM-generated mapping and falls back
to these only when no LLM is configured or the LLM's mapping fails, so the sample data can
always be ingested. They are never written to `schema_mappings`, so a configured LLM still
gets to map the format later.
"""

import json
from dataclasses import dataclass
from functools import cache
from importlib.resources import files

from cmdb.ingest.mapping import MappingSpec


@dataclass(frozen=True)
class BundledMapping:
    name: str
    fingerprint: str
    record_path: str
    spec: MappingSpec


@cache
def bundled_mappings() -> dict[str, BundledMapping]:
    """All bundled mappings by fingerprint."""
    mappings: dict[str, BundledMapping] = {}
    for entry in files("cmdb.ingest").joinpath("bundled_mappings").iterdir():
        if not entry.name.endswith(".json"):
            continue
        data = json.loads(entry.read_text(encoding="utf-8"))
        mappings[data["fingerprint"]] = BundledMapping(
            name=entry.name.removesuffix(".json"),
            fingerprint=data["fingerprint"],
            record_path=data["record_path"],
            spec=MappingSpec.model_validate(data["spec"]),
        )
    return mappings


def bundled_mapping(fingerprint: str) -> MappingSpec | None:
    bundled = bundled_mappings().get(fingerprint)
    return bundled.spec if bundled is not None else None
