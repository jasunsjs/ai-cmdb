from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import pytest

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
from cmdb.models.enums import AppType, CIStatus, IdentifierKind

ALL_NORMALIZERS = [
    normalize_text,
    normalize_lower_text,
    normalize_list,
    normalize_bool,
    normalize_encryption,
    normalize_status,
    normalize_app_type,
    normalize_datetime,
    normalize_email,
    normalize_mac,
    normalize_ip,
    normalize_int,
]


@pytest.mark.parametrize("raw", [None, "", "   ", "null", "None", "N/A", "na", "-"])
def test_missing_values(raw: Any) -> None:
    assert is_missing(raw)


@pytest.mark.parametrize("normalizer", ALL_NORMALIZERS)
@pytest.mark.parametrize("raw", [None, "", "n/a"])
def test_every_normalizer_returns_none_for_missing(
    normalizer: Callable[[Any], Any], raw: Any
) -> None:
    assert normalizer(raw) is None


@pytest.mark.parametrize(
    ("normalizer", "raw"),
    [
        (normalize_text, {"a": 1}),
        (normalize_list, 42),
        (normalize_bool, "maybe"),
        (normalize_bool, 2),
        (normalize_encryption, "APFS"),
        (normalize_status, "In Repair"),
        (normalize_status, 1),
        (normalize_app_type, "hybrid"),
        (normalize_datetime, "last tuesday"),
        (normalize_datetime, 1721642400),
        (normalize_email, "not-an-email"),
        (normalize_mac, "AA:BB:CC"),
        (normalize_ip, "10.10.22"),
        (normalize_int, "about 50"),
        (normalize_int, True),
        (normalize_int, 1.5),
    ],
)
def test_unclassifiable_values_raise(normalizer: Callable[[Any], Any], raw: Any) -> None:
    with pytest.raises(UnrecognizedValueError):
        normalizer(raw)


# --- text and lists ---


def test_text_trims_and_collapses_whitespace() -> None:
    assert normalize_text("  New York   HQ ") == "New York HQ"
    assert normalize_lower_text(" Laptop-JDOE ") == "laptop-jdoe"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (["GitHub", " Slack ", "Salesforce"], ["GitHub", "Slack", "Salesforce"]),
        ("GitHub; Jira", ["GitHub", "Jira"]),
        ("GitHub, Jira | Zoom", ["GitHub", "Jira", "Zoom"]),
        (["Slack", "slack", ""], ["Slack"]),
        ([], []),
    ],
)
def test_normalize_list(raw: Any, expected: list[str]) -> None:
    assert normalize_list(raw) == expected


# --- booleans and encryption ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(True, True), (False, False), ("true", True), ("FALSE", False), ("yes", True),
     ("no", False), ("Enabled", True), ("off", False), (1, True), (0, False)],
)  # fmt: skip
def test_normalize_bool(raw: Any, expected: bool) -> None:
    assert normalize_bool(raw) is expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("FileVault Enabled", True),  # sample_hardware.csv
        ("LUKS Full Disk Encryption", True),  # sample_hardware.csv
        ("FileVault 2", True),  # sample_hardware.csv
        ("true", True),  # sample_hardware.yaml (text via BaseLoader)
        (True, True),
        ("BitLocker", True),
        ("FileVault Disabled", False),
        ("Not Encrypted", False),
        ("unencrypted", False),
        ("No encryption", False),
    ],
)
def test_normalize_encryption(raw: Any, expected: bool) -> None:
    assert normalize_encryption(raw) is expected


# --- enums ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("active", CIStatus.ACTIVE),
        ("ACTIVE", CIStatus.ACTIVE),  # sample_okta.json
        ("inactive", CIStatus.INACTIVE),  # sample_hardware.json
        ("deactivated", CIStatus.INACTIVE),  # sample_hardware.yaml
        ("SUSPENDED", CIStatus.SUSPENDED),  # sample_okta.json
        ("LOCKED_OUT", CIStatus.SUSPENDED),
        ("Decommissioned", CIStatus.INACTIVE),
        ("unknown", CIStatus.UNKNOWN),
    ],
)
def test_normalize_status(raw: str, expected: CIStatus) -> None:
    assert normalize_status(raw) is expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("SaaS", AppType.SAAS),  # sample_app.json
        ("cloud", AppType.SAAS),
        ("cloud-hosted", AppType.SAAS),
        ("on-premise", AppType.ON_PREM),  # sample_app.json
        ("on-prem", AppType.ON_PREM),
        ("self-hosted", AppType.ON_PREM),  # sample_app.json deployment
        ("On Premises", AppType.ON_PREM),
    ],
)
def test_normalize_app_type(raw: str, expected: AppType) -> None:
    assert normalize_app_type(raw) is expected


# --- operating systems ---


@pytest.mark.parametrize(
    ("name", "version", "expected"),
    [
        ("macos", None, ("macOS", None)),  # sample_hardware.json
        ("macOS", None, ("macOS", None)),
        ("macOS Ventura", None, ("macOS", "13")),  # sample_hardware.csv
        ("macOS Sonoma 14.5", None, ("macOS", "14.5")),  # sample_hardware.csv
        ("Ubuntu 22", None, ("Ubuntu", "22")),  # sample_hardware.json
        ("Ubuntu 22.04 LTS", None, ("Ubuntu", "22.04")),  # sample_hardware.csv
        ("macOS", "Ventura 13.5", ("macOS", "13.5")),  # sample_hardware.yaml
        ("Ubuntu", "22.04 LTS", ("Ubuntu", "22.04")),  # sample_hardware.yaml
        ("Mac OS X 10.15", None, ("macOS", "10.15")),
        ("Windows 11 Pro", None, ("Windows", "11")),
        ("Red Hat Enterprise Linux 9.2", None, ("RHEL", "9.2")),
        ("iOS 17.5", None, ("iOS", "17.5")),
        (None, None, (None, None)),
    ],
)
def test_normalize_os(name: Any, version: Any, expected: tuple[str | None, str | None]) -> None:
    assert normalize_os(name, version) == expected


def test_unknown_os_raises() -> None:
    with pytest.raises(UnrecognizedValueError):
        normalize_os("Plan 9")


# --- timestamps ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2024-07-22T10:00:00Z", datetime(2024, 7, 22, 10, 0, tzinfo=UTC)),
        ("2024-07-22T12:00:00+02:00", datetime(2024, 7, 22, 10, 0, tzinfo=UTC)),
        ("2024-07-15", datetime(2024, 7, 15, tzinfo=UTC)),
        ("2024-07-15 10:03:00", datetime(2024, 7, 15, 10, 3, tzinfo=UTC)),  # naive -> UTC
        (datetime(2024, 7, 15, 10, 3), datetime(2024, 7, 15, 10, 3, tzinfo=UTC)),
        (date(2024, 7, 15), datetime(2024, 7, 15, tzinfo=UTC)),
    ],
)
def test_normalize_datetime(raw: Any, expected: datetime) -> None:
    result = normalize_datetime(raw)

    assert result == expected
    assert result.tzinfo is UTC


# --- network and contact values ---


def test_normalize_email_lowercases() -> None:
    assert normalize_email(" J.Doe@Example.com ") == "j.doe@example.com"


@pytest.mark.parametrize(
    "raw", ["AA:BB:CC:DD:11:22", "aa-bb-cc-dd-11-22", "aabb.ccdd.1122", "aabbccdd1122"]
)
def test_normalize_mac(raw: str) -> None:
    assert normalize_mac(raw) == "AA:BB:CC:DD:11:22"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("10.10.22.5", "10.10.22.5"), (" 10.10.22.45 ", "10.10.22.45"),
     ("2001:0db8:0000:0000:0000:0000:0000:0001", "2001:db8::1")],
)  # fmt: skip
def test_normalize_ip(raw: str, expected: str) -> None:
    assert normalize_ip(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"), [(52, 52), ("52", 52), ("1,200", 1200), (15.0, 15), ("-3", -3)]
)
def test_normalize_int(raw: Any, expected: int) -> None:
    assert normalize_int(raw) == expected


# --- identifiers ---


@pytest.mark.parametrize(
    ("kind", "raw", "expected"),
    [
        (IdentifierKind.SERIAL, "LAP-JD-20230615", "lap-jd-20230615"),
        (IdentifierKind.ASSET_TAG, " C-19283 ", "c-19283"),
        (IdentifierKind.EMAIL, "John.Doe@Example.com", "john.doe@example.com"),
        (IdentifierKind.MAC, "aa-bb-cc-dd-11-22", "AA:BB:CC:DD:11:22"),
        (IdentifierKind.ALIAS, "GitHub  Enterprise", "github enterprise"),
        (IdentifierKind.EXTERNAL_ID, "u_110", "u_110"),
    ],
)
def test_normalize_identifier(kind: IdentifierKind, raw: str, expected: str) -> None:
    assert normalize_identifier(kind, raw) == expected
