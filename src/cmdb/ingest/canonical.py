"""The canonical vocabulary: which fields, identifiers and relations each CI type supports.

Every source format is mapped *into* this vocabulary (the mappings live in `schema_mappings`).
It is defined once, here, because each entry is tied to code: a normalizer and a place to
store the result. The Phase 4 mapping prompt is built from these descriptions, and LLM
mappings are validated against them.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from cmdb.ingest.errors import UnrecognizedValueError
from cmdb.ingest.normalizers import (
    is_missing,
    normalize_app_type,
    normalize_bool,
    normalize_datetime,
    normalize_email,
    normalize_encryption,
    normalize_identifier,
    normalize_int,
    normalize_ip,
    normalize_list,
    normalize_lower_text,
    normalize_mac,
    normalize_os,
    normalize_status,
    normalize_text,
)
from cmdb.models.enums import CIType, IdentifierKind, RelType

Normalizer = Callable[[Any], Any]


@dataclass(frozen=True)
class FieldSpec:
    normalizer: Normalizer
    description: str
    # Also register the normalized value as this kind of identifier in ci_identifiers.
    identifier: IdentifierKind | None = None
    # False for identifier-only keys that have no column of their own.
    stored: bool = True


class Direction(StrEnum):
    OUTGOING = "outgoing"  # this CI is the relationship's source
    INCOMING = "incoming"  # this CI is the relationship's target


@dataclass(frozen=True)
class RelationSpec:
    rel_type: RelType
    target: CIType
    direction: Direction
    many: bool
    description: str


# Raw OS inputs; combined by normalize_os into the stored os_family / os_version columns.
_OS_INPUTS = ("os", "os_name", "os_version")

CANONICAL_FIELDS: dict[CIType, dict[str, FieldSpec]] = {
    CIType.DEVICE: {
        "name": FieldSpec(normalize_text, "Display name; defaults to the hostname"),
        "hostname": FieldSpec(
            normalize_lower_text, "Network hostname, e.g. laptop-jdoe", IdentifierKind.HOSTNAME
        ),
        "ip_address": FieldSpec(normalize_ip, "Primary IPv4 or IPv6 address"),
        "mac_address": FieldSpec(normalize_mac, "Primary MAC address", IdentifierKind.MAC),
        "os": FieldSpec(
            normalize_text, "Operating system as one string, e.g. 'macOS Sonoma 14.5'", stored=False
        ),
        "os_name": FieldSpec(
            normalize_text, "Operating system name when separate from its version", stored=False
        ),
        "os_version": FieldSpec(
            normalize_text, "Operating system version when separate from its name", stored=False
        ),
        "location": FieldSpec(normalize_text, "Physical site or office"),
        "encryption_enabled": FieldSpec(
            normalize_encryption, "Whether full-disk encryption is enabled"
        ),
        "device_type": FieldSpec(normalize_lower_text, "Kind of device, e.g. laptop or server"),
        "serial_number": FieldSpec(
            normalize_text, "Manufacturer serial number", IdentifierKind.SERIAL
        ),
        "last_seen": FieldSpec(normalize_datetime, "Last check-in time"),
        "status": FieldSpec(normalize_status, "Lifecycle status"),
        "asset_tag": FieldSpec(
            normalize_text,
            "Organization-wide asset tag or device ID",
            IdentifierKind.ASSET_TAG,
            stored=False,
        ),
        "external_id": FieldSpec(
            normalize_text,
            "The source system's own ID for this device",
            IdentifierKind.EXTERNAL_ID,
            stored=False,
        ),
    },
    CIType.USER: {
        "name": FieldSpec(normalize_text, "Full name (never used as an identifier)"),
        "email": FieldSpec(normalize_email, "Email address", IdentifierKind.EMAIL),
        "department": FieldSpec(normalize_text, "Department, e.g. Engineering"),
        "team": FieldSpec(normalize_text, "Team within the department"),
        "groups": FieldSpec(normalize_list, "Directory or SSO groups"),
        "mfa_enabled": FieldSpec(normalize_bool, "Whether multi-factor authentication is on"),
        "last_login": FieldSpec(normalize_datetime, "Last login time"),
        "status": FieldSpec(normalize_status, "Account status"),
        "employee_id": FieldSpec(
            normalize_text, "HR employee ID", IdentifierKind.EMPLOYEE_ID, stored=False
        ),
        "external_id": FieldSpec(
            normalize_text,
            "The source system's own ID for this user, e.g. an Okta user ID",
            IdentifierKind.EXTERNAL_ID,
            stored=False,
        ),
    },
    CIType.APP: {
        "name": FieldSpec(normalize_text, "Application name", IdentifierKind.ALIAS),
        "vendor": FieldSpec(normalize_text, "Vendor or publisher"),
        "app_type": FieldSpec(
            normalize_app_type, "saas (vendor-run) or on_prem (operated by us, wherever hosted)"
        ),
        "category": FieldSpec(normalize_text, "Category, e.g. Collaboration"),
        "owner_team": FieldSpec(normalize_text, "Team that owns the application"),
        "reported_usage_count": FieldSpec(normalize_int, "Number of users reported by the source"),
        "sso_enabled": FieldSpec(normalize_bool, "Whether single sign-on is enabled"),
        "status": FieldSpec(normalize_status, "Lifecycle status"),
        "external_id": FieldSpec(
            normalize_text,
            "The source system's own ID for this app",
            IdentifierKind.EXTERNAL_ID,
            stored=False,
        ),
    },
}

CANONICAL_RELATIONS: dict[CIType, dict[str, RelationSpec]] = {
    CIType.DEVICE: {
        "owner": RelationSpec(
            RelType.OWNS,
            CIType.USER,
            Direction.INCOMING,
            many=False,
            description="The user the device is assigned to",
        ),
    },
    CIType.USER: {
        "apps": RelationSpec(
            RelType.USES,
            CIType.APP,
            Direction.OUTGOING,
            many=True,
            description="Applications assigned to or used by the user",
        ),
    },
    CIType.APP: {
        "integrations": RelationSpec(
            RelType.INTEGRATES_WITH,
            CIType.APP,
            Direction.OUTGOING,
            many=True,
            description="Other applications this application integrates with",
        ),
    },
}


@dataclass
class NormalizedRecord:
    ci_type: CIType
    # Stored fields that normalized cleanly (column name -> value).
    values: dict[str, Any] = field(default_factory=dict)
    # (kind, canonical value) pairs for ci_identifiers.
    identifiers: list[tuple[IdentifierKind, str]] = field(default_factory=list)
    # Related records by relation name, e.g. {"owner": [<user>]}.
    relations: dict[str, list["NormalizedRecord"]] = field(default_factory=dict)
    # (field, raw value) pairs the rules couldn't classify.
    unrecognized: list[tuple[str, Any]] = field(default_factory=list)
    # Fields outside the vocabulary (field -> raw value); stored in `attributes`.
    unknown_fields: dict[str, Any] = field(default_factory=dict)

    def is_empty(self) -> bool:
        return not (self.values or self.identifiers or self.relations)


def normalize_record(ci_type: CIType, values: Mapping[str, Any]) -> NormalizedRecord:
    """Normalize one record already expressed in canonical field names.

    Bad values are collected in `unrecognized` rather than raised, so one bad field never
    drops the whole record.
    """
    fields = CANONICAL_FIELDS[ci_type]
    relations = CANONICAL_RELATIONS[ci_type]
    result = NormalizedRecord(ci_type)
    os_inputs: dict[str, str] = {}

    for name, raw in values.items():
        if name in relations:
            _add_relation(result, name, relations[name], raw)
            continue
        spec = fields.get(name)
        if spec is None:
            result.unknown_fields[name] = raw
            continue
        try:
            value = spec.normalizer(raw)
        except UnrecognizedValueError:
            result.unrecognized.append((name, raw))
            continue
        if value is None:
            continue
        if name in _OS_INPUTS:
            os_inputs[name] = value
            continue
        if spec.identifier is not None:
            identifier = normalize_identifier(spec.identifier, value)
            if identifier is not None:
                result.identifiers.append((spec.identifier, identifier))
        if spec.stored:
            result.values[name] = value

    if ci_type is CIType.DEVICE:
        _add_os(result, os_inputs)
        if "name" not in result.values and "hostname" in result.values:
            result.values["name"] = result.values["hostname"]
    return result


def set_field(record: NormalizedRecord, field_name: str, value: Any) -> None:
    """Apply a value resolved after normalization (e.g. classified by the LLM).

    `value` must already be normalized; for "os" it is an (os_family, os_version) pair.
    The field's `unrecognized` entry is removed either way.
    """
    record.unrecognized = [(f, raw) for f, raw in record.unrecognized if f != field_name]
    if value is None:
        return
    if field_name == "os":
        family, version = value
        if family is not None:
            record.values["os_family"] = family
        if version is not None:
            record.values["os_version"] = version
        return
    spec = CANONICAL_FIELDS[record.ci_type][field_name]
    if spec.identifier is not None:
        identifier = normalize_identifier(spec.identifier, value)
        if identifier is not None and (spec.identifier, identifier) not in record.identifiers:
            record.identifiers.append((spec.identifier, identifier))
    if spec.stored:
        record.values[field_name] = value


def to_jsonable(record: NormalizedRecord) -> dict[str, Any]:
    """A JSON-safe dump of a normalized record (for source_records.normalized)."""
    return {
        "ci_type": record.ci_type.value,
        "values": {key: jsonable_value(value) for key, value in record.values.items()},
        "identifiers": [[kind.value, value] for kind, value in record.identifiers],
        "relations": {
            name: [to_jsonable(related) for related in items]
            for name, items in record.relations.items()
        },
        "unrecognized": [[name, raw] for name, raw in record.unrecognized],
    }


def jsonable_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return value.value
    return value


def _add_os(result: NormalizedRecord, os_inputs: dict[str, str]) -> None:
    if not os_inputs:
        return
    name = os_inputs.get("os") or os_inputs.get("os_name")
    try:
        family, version = normalize_os(name, os_inputs.get("os_version"))
    except UnrecognizedValueError as exc:
        result.unrecognized.append(("os", exc.value))
        return
    if family is not None:
        result.values["os_family"] = family
    if version is not None:
        result.values["os_version"] = version


def _add_relation(result: NormalizedRecord, name: str, spec: RelationSpec, raw: Any) -> None:
    """Normalize related records: nested mappings, or plain names ("GitHub" -> {name: ...})."""
    if is_missing(raw):
        return
    if isinstance(raw, Mapping):
        items: list[Any] = [raw]
    elif isinstance(raw, list):
        items = raw
    elif isinstance(raw, str) and spec.many:
        try:
            items = normalize_list(raw) or []
        except UnrecognizedValueError:
            result.unrecognized.append((name, raw))
            return
    else:
        items = [raw]

    if not spec.many and len(items) > 1:
        result.unrecognized.append((name, raw))
        return

    related = []
    for item in items:
        values = item if isinstance(item, Mapping) else {"name": item}
        record = normalize_record(spec.target, values)
        if not record.is_empty():
            related.append(record)
    if related:
        result.relations[name] = related
