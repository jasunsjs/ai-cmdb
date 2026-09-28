"""Pure value normalizers.

Every normalizer takes a raw value (a string from CSV/YAML, or a native JSON value) and
returns the canonical value, or None when the input is missing. A value that is present but
can't be classified raises UnrecognizedValueError: the rules never guess.
"""

import ipaddress
import re
from datetime import UTC, date, datetime
from typing import Any

from cmdb.ingest.errors import UnrecognizedValueError
from cmdb.models.enums import AppType, CIStatus, IdentifierKind

_MISSING = {"", "null", "none", "n/a", "na", "-"}


def is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in _MISSING
    return False


def _key(value: str) -> str:
    """Lowercase, treat - and _ as spaces, collapse whitespace: "LOCKED_OUT" -> "locked out"."""
    return " ".join(re.sub(r"[-_]", " ", value).lower().split())


# --- text ---


def normalize_text(value: Any) -> str | None:
    if is_missing(value):
        return None
    if isinstance(value, dict | list):
        raise UnrecognizedValueError(value, "text")
    return " ".join(str(value).split())


def normalize_lower_text(value: Any) -> str | None:
    text = normalize_text(value)
    return text.lower() if text is not None else None


def normalize_list(value: Any) -> list[str] | None:
    """A list of strings. CSV cells like "GitHub; Jira" are split on , ; or |."""
    if is_missing(value):
        return None
    if isinstance(value, str):
        items: list[Any] = re.split(r"[,;|]", value)
    elif isinstance(value, list):
        items = value
    else:
        raise UnrecognizedValueError(value, "list")

    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = normalize_text(item)
        if text is not None and text.lower() not in seen:
            seen.add(text.lower())
            result.append(text)
    return result


# --- booleans ---

_TRUE = {"true", "yes", "y", "1", "enabled", "on"}
_FALSE = {"false", "no", "n", "0", "disabled", "off"}


def normalize_bool(value: Any) -> bool | None:
    if is_missing(value):
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        key = value.strip().lower()
        if key in _TRUE:
            return True
        if key in _FALSE:
            return False
    raise UnrecognizedValueError(value, "boolean")


_ENCRYPTION_NEGATION = re.compile(r"\b(not|no|none|unencrypted|disabled|off)\b")
_ENCRYPTION_TECH = re.compile(r"filevault|bitlocker|luks|full[ -]?disk|encrypt|\bfde\b")


def normalize_encryption(value: Any) -> bool | None:
    """Disk encryption state. Naming a technology ("FileVault 2") implies it is enabled."""
    try:
        return normalize_bool(value)
    except UnrecognizedValueError:
        if not isinstance(value, str):
            raise
    key = value.lower()
    if _ENCRYPTION_NEGATION.search(key):
        return False
    if _ENCRYPTION_TECH.search(key):
        return True
    raise UnrecognizedValueError(value, "encryption state")


# --- enums ---

_STATUS_SYNONYMS = {
    CIStatus.ACTIVE: {"active", "enabled", "online", "in use", "deployed"},
    CIStatus.INACTIVE: {
        "inactive",
        "deactivated",
        "disabled",
        "offline",
        "retired",
        "decommissioned",
        "deprovisioned",
        "terminated",
    },
    CIStatus.SUSPENDED: {"suspended", "locked", "locked out"},
    CIStatus.UNKNOWN: {"unknown"},
}
_STATUS_LOOKUP = {syn: status for status, syns in _STATUS_SYNONYMS.items() for syn in syns}


def normalize_status(value: Any) -> CIStatus | None:
    if is_missing(value):
        return None
    if isinstance(value, str) and (status := _STATUS_LOOKUP.get(_key(value))):
        return status
    raise UnrecognizedValueError(value, "status")


_APP_TYPE_SYNONYMS = {
    AppType.SAAS: {"saas", "cloud", "cloud hosted", "hosted", "software as a service"},
    # "On-prem" means we operate it, wherever it runs (self-hosted on AWS counts).
    AppType.ON_PREM: {
        "on premise",
        "on premises",
        "on prem",
        "onprem",
        "self hosted",
        "self managed",
    },
}
_APP_TYPE_LOOKUP = {syn: kind for kind, syns in _APP_TYPE_SYNONYMS.items() for syn in syns}


def normalize_app_type(value: Any) -> AppType | None:
    if is_missing(value):
        return None
    if isinstance(value, str) and (app_type := _APP_TYPE_LOOKUP.get(_key(value))):
        return app_type
    raise UnrecognizedValueError(value, "app type")


# --- operating systems ---

# Checked in order; more specific patterns first.
_OS_FAMILIES = [
    ("macOS", re.compile(r"mac\s*os|\bosx\b|\bos\s*x\b|darwin")),
    ("ChromeOS", re.compile(r"chrome\s*os")),
    ("iOS", re.compile(r"\bios\b|ipados")),
    ("Windows", re.compile(r"windows|\bwin\s*\d+\b")),
    ("Ubuntu", re.compile(r"ubuntu")),
    ("Debian", re.compile(r"debian")),
    ("RHEL", re.compile(r"\brhel\b|red\s*hat")),
    ("CentOS", re.compile(r"centos")),
    ("Fedora", re.compile(r"fedora")),
    ("Android", re.compile(r"android")),
    ("Linux", re.compile(r"linux")),
]
_MACOS_CODENAMES = {
    "big sur": "11",
    "monterey": "12",
    "ventura": "13",
    "sonoma": "14",
    "sequoia": "15",
    "tahoe": "26",
}
_VERSION_NUMBER = re.compile(r"\d+(?:\.\d+)*")


def normalize_os(name: Any, version: Any = None) -> tuple[str | None, str | None]:
    """(os_family, os_version) from a combined string or separate name/version fields.

    "Ubuntu 22.04 LTS" -> ("Ubuntu", "22.04"); "macOS Ventura" -> ("macOS", "13");
    ("macOS", "Ventura 13.5") -> ("macOS", "13.5").
    """
    name_text, version_text = normalize_text(name), normalize_text(version)
    if name_text is None and version_text is None:
        return None, None
    combined = " ".join(part for part in (name_text, version_text) if part)
    lowered = combined.lower()

    family = next((fam for fam, pattern in _OS_FAMILIES if pattern.search(lowered)), None)
    if family is None:
        raise UnrecognizedValueError(combined, "operating system")

    # Prefer a number from the explicit version field, then from the name.
    for text in (version_text, name_text):
        if text and (match := _VERSION_NUMBER.search(text)):
            return family, match.group()
    if family == "macOS":
        for codename, major in _MACOS_CODENAMES.items():
            if codename in lowered:
                return family, major
    return family, None


# --- timestamps ---


def normalize_datetime(value: Any) -> datetime | None:
    """A UTC-aware datetime. Date-only values mean midnight; naive values are assumed UTC."""
    if is_missing(value):
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day)
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError as exc:
            raise UnrecognizedValueError(value, "datetime") from exc
    else:
        raise UnrecognizedValueError(value, "datetime")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


# --- network and contact identifiers ---

_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def normalize_email(value: Any) -> str | None:
    if is_missing(value):
        return None
    if isinstance(value, str):
        email = value.strip().lower()
        if _EMAIL.fullmatch(email):
            return email
    raise UnrecognizedValueError(value, "email address")


def normalize_mac(value: Any) -> str | None:
    """Upper-case, colon-separated: aa-bb-cc-dd-11-22 -> AA:BB:CC:DD:11:22."""
    if is_missing(value):
        return None
    if isinstance(value, str):
        digits = re.sub(r"[:\-.\s]", "", value)
        if re.fullmatch(r"[0-9a-fA-F]{12}", digits):
            return ":".join(digits[i : i + 2] for i in range(0, 12, 2)).upper()
    raise UnrecognizedValueError(value, "MAC address")


def normalize_ip(value: Any) -> str | None:
    if is_missing(value):
        return None
    if isinstance(value, str):
        try:
            return str(ipaddress.ip_address(value.strip()))
        except ValueError:
            pass
    raise UnrecognizedValueError(value, "IP address")


# --- numbers ---


def normalize_int(value: Any) -> int | None:
    if is_missing(value):
        return None
    if isinstance(value, bool):
        raise UnrecognizedValueError(value, "integer")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        digits = re.sub(r"[,_\s]", "", value)
        if re.fullmatch(r"-?\d+", digits):
            return int(digits)
    raise UnrecognizedValueError(value, "integer")


# --- identifiers ---


def normalize_identifier(kind: IdentifierKind, value: Any) -> str | None:
    """The canonical form stored in ci_identifiers.value, so equal keys compare equal."""
    match kind:
        case IdentifierKind.EMAIL:
            return normalize_email(value)
        case IdentifierKind.MAC:
            return normalize_mac(value)
        case _:
            return normalize_lower_text(value)
