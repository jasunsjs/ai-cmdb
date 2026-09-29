"""LLM-generated schema mappings for source formats seen for the first time."""

import json
from typing import Any

from pydantic import BaseModel, Field

from cmdb.ai.client import LLMClient, LLMError
from cmdb.ingest.canonical import CANONICAL_FIELDS, CANONICAL_RELATIONS
from cmdb.ingest.errors import MappingGenerationError
from cmdb.ingest.mapping import MappingContext, MappingSpec, RelatedMapping, validate_mapping
from cmdb.models import CIType

_MAX_VALUE_CHARS = 80
_MAX_METADATA_ITEMS = 40

SYSTEM_PROMPT = """\
You map fields from an IT inventory export into a Configuration Management Database (CMDB).
Each record in the file describes one configuration item (CI). Pick the CI type the records
describe and map source fields to that type's canonical fields.

Rules:
- Use only the canonical fields and relations listed below.
- Each mapping pairs a canonical "field" with the "source" key path that holds its value.
- Use source key paths exactly as listed (nested fields use dots, e.g. network.primary_ip).
- Omit canonical fields with no good source. Unmapped source fields are kept automatically.
- Put information about *other* entities under "related": the user a device is assigned to
  is the device's "owner"; a user's list of application names is "apps"; an application's
  list of integrated application names is "integrations". For list relations map only the
  field "name", with the path of the list as its source.
- A related entity with several source fields (e.g. an owner block with a name, email and
  employee ID) maps each of them to the related CI type's own canonical fields: map as many
  as exist, since they identify the entity across systems.
- "name" must hold a human-readable name. Never map an email address, ID or code to "name";
  map it to the matching field (email, employee_id, asset_tag, ...) instead.
- "observed_at" is the source path of when the source system last observed or synced the
  record (e.g. a device's last check-in). It is never a user activity time such as
  last_login. Use null if there is none.
- "asset_tag" is an organization-wide device tag or device ID shared across tools.
  "external_id" is an ID that is only meaningful inside the system that produced the file
  (e.g. an Okta user ID or an app ID in one tool's export).
- "source_system" is a short lowercase slug for the system that produced the file, inferred
  from the metadata or the file name (e.g. "okta"). Use null if you can't tell. It is
  required whenever you map external_id.

Canonical vocabulary:
{vocabulary}
"""


class FieldPair(BaseModel):
    field: str = Field(description="Canonical field name")
    source: str = Field(description="Source key path, exactly as listed")


class RelatedReply(BaseModel):
    relation: str = Field(description="owner, apps or integrations")
    fields: list[FieldPair]


class MappingReply(BaseModel):
    """What the LLM returns. Mappings are lists of pairs rather than JSON objects with free-form
    keys, because providers enforcing structured outputs drop such keys."""

    ci_type: CIType
    fields: list[FieldPair]
    related: list[RelatedReply] = Field(default_factory=list)
    observed_at: str | None = Field(
        default=None, description="Source path of when the source last observed the record"
    )
    source_system: str | None = Field(
        default=None, description="Short lowercase slug of the producing system, or null"
    )

    def to_spec(self) -> MappingSpec:
        return MappingSpec(
            ci_type=self.ci_type,
            fields={pair.field: pair.source for pair in self.fields},
            related=[
                RelatedMapping(
                    relation=related.relation,
                    fields={pair.field: pair.source for pair in related.fields},
                )
                for related in self.related
            ],
            observed_at=self.observed_at,
            source_system=self.source_system,
        )


class LLMMappingGenerator:
    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def generate(self, context: MappingContext) -> MappingSpec:
        system = SYSTEM_PROMPT.replace("{vocabulary}", describe_vocabulary())
        user = describe_context(context)
        errors: list[str] = []
        for attempt in (1, 2):
            prompt = user
            if errors:
                prompt += (
                    "\n\nYour previous mapping had these problems; return a corrected mapping:\n"
                    + "\n".join(f"- {error}" for error in errors)
                )
            try:
                reply = self.llm.complete_json(
                    system=system, user=prompt, schema=MappingReply, purpose="schema_mapping"
                )
            except LLMError as exc:
                raise MappingGenerationError(str(exc)) from exc
            spec = reply.to_spec()
            errors = validate_mapping(spec, context.key_paths)
            if not errors:
                return spec
            if attempt == 2:
                break
        raise MappingGenerationError("; ".join(errors))


def describe_vocabulary() -> str:
    lines: list[str] = []
    for ci_type, fields in CANONICAL_FIELDS.items():
        lines.append(f"\nCI type '{ci_type.value}':")
        for name, spec in fields.items():
            identifier = f" [identifier: {spec.identifier.value}]" if spec.identifier else ""
            lines.append(f"  - {name}: {spec.description}{identifier}")
        for name, relation in CANONICAL_RELATIONS[ci_type].items():
            target = relation.target.value
            if relation.many:
                shape = f"list of {target} names; map only 'name'"
            else:
                target_fields = ", ".join(
                    f for f in CANONICAL_FIELDS[relation.target] if not f.startswith("os")
                )
                shape = f"one {target}; map any of its fields: {target_fields}"
            lines.append(f"  - related '{name}' ({shape}): {relation.description}")
    return "\n".join(lines)


def describe_context(context: MappingContext) -> str:
    lines = [
        f"File name: {context.filename}",
        f"Records are at: {context.record_path or '(the document root)'}",
        "",
        "Source key paths with example values:",
    ]
    for path in context.key_paths:
        examples = ", ".join(_short(value) for value in context.samples.get(path, []))
        lines.append(f"  - {path}: {examples}")
    if context.metadata:
        lines += ["", "File metadata (outside the records):"]
        for key, value in list(context.metadata.items())[:_MAX_METADATA_ITEMS]:
            lines.append(f"  - {key}: {_short(value)}")
    return "\n".join(lines)


def _short(value: Any) -> str:
    text = json.dumps(value, default=str)
    return text if len(text) <= _MAX_VALUE_CHARS else text[: _MAX_VALUE_CHARS - 3] + "..."
