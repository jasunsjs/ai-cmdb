from datetime import UTC, datetime

from cmdb.ingest.canonical import (
    CANONICAL_FIELDS,
    CANONICAL_RELATIONS,
    NormalizedRecord,
    normalize_record,
)
from cmdb.models.enums import AppType, CIStatus, CIType, IdentifierKind

# John's laptop as a CSV row, already renamed to canonical fields (all values are strings).
CSV_DEVICE = {
    "hostname": "laptop-jdoe",
    "asset_tag": "C-19283",
    "ip_address": "10.10.22.5",
    "mac_address": "AA:BB:CC:DD:11:22",
    "os": "macOS Ventura",
    "location": "London",
    "encryption_enabled": "FileVault Enabled",
    "status": "active",
    "device_type": "laptop",
    "serial_number": "LAP-JD-20230615",
    "last_seen": "2024-07-22T10:00:00Z",
    "owner": "John D.",
}


def identifiers(record: NormalizedRecord) -> set[tuple[IdentifierKind, str]]:
    return set(record.identifiers)


def test_every_ci_type_has_a_vocabulary() -> None:
    assert set(CANONICAL_FIELDS) == set(CIType)
    assert set(CANONICAL_RELATIONS) == set(CIType)


def test_csv_device_row_normalizes_to_typed_values() -> None:
    record = normalize_record(CIType.DEVICE, CSV_DEVICE)

    assert record.values == {
        "name": "laptop-jdoe",  # defaults to the hostname
        "hostname": "laptop-jdoe",
        "ip_address": "10.10.22.5",
        "mac_address": "AA:BB:CC:DD:11:22",
        "os_family": "macOS",
        "os_version": "13",
        "location": "London",
        "encryption_enabled": True,
        "status": CIStatus.ACTIVE,
        "device_type": "laptop",
        "serial_number": "LAP-JD-20230615",
        "last_seen": datetime(2024, 7, 22, 10, 0, tzinfo=UTC),
    }
    assert record.unrecognized == []
    assert record.unknown_fields == {}


def test_device_identifiers_are_derived_from_fields() -> None:
    record = normalize_record(CIType.DEVICE, CSV_DEVICE)

    assert identifiers(record) == {
        (IdentifierKind.HOSTNAME, "laptop-jdoe"),
        (IdentifierKind.MAC, "AA:BB:CC:DD:11:22"),
        (IdentifierKind.SERIAL, "lap-jd-20230615"),
        (IdentifierKind.ASSET_TAG, "c-19283"),  # identifier-only: not in values
    }
    assert "asset_tag" not in record.values


def test_owner_given_only_as_a_name_has_no_identifiers() -> None:
    record = normalize_record(CIType.DEVICE, CSV_DEVICE)

    [owner] = record.relations["owner"]
    assert owner.ci_type is CIType.USER
    assert owner.values == {"name": "John D."}
    assert owner.identifiers == []


def test_nested_owner_becomes_a_user_with_identifiers() -> None:
    yaml_device = {
        "hostname": "laptop-jdoe",
        "os_name": "macOS",
        "os_version": "Ventura 13.5",
        "owner": {
            "name": "John Doe",
            "email": "john.doe@example.com",
            "employee_id": "EMP-2156",
            "department": "Engineering",
            "team": "Platform Engineering",
        },
    }

    record = normalize_record(CIType.DEVICE, yaml_device)

    assert (record.values["os_family"], record.values["os_version"]) == ("macOS", "13.5")
    [owner] = record.relations["owner"]
    assert owner.values["team"] == "Platform Engineering"
    assert identifiers(owner) == {
        (IdentifierKind.EMAIL, "john.doe@example.com"),
        (IdentifierKind.EMPLOYEE_ID, "emp-2156"),
    }


def test_okta_user_with_app_list() -> None:
    okta_user = {
        "external_id": "u_110",
        "name": "Jane Doe",
        "email": "jane.d@example.com",
        "groups": ["Engineering", "Admins"],
        "apps": ["GitHub", "Slack", "Salesforce"],
        "mfa_enabled": True,
        "last_login": "2024-07-15T10:03:00Z",
        "status": "ACTIVE",
    }

    record = normalize_record(CIType.USER, okta_user)

    # A user's name is never an identifier; email and the Okta ID are.
    assert identifiers(record) == {
        (IdentifierKind.EMAIL, "jane.d@example.com"),
        (IdentifierKind.EXTERNAL_ID, "u_110"),
    }
    assert record.values["status"] is CIStatus.ACTIVE
    apps = record.relations["apps"]
    assert [app.values["name"] for app in apps] == ["GitHub", "Slack", "Salesforce"]
    assert apps[0].identifiers == [(IdentifierKind.ALIAS, "github")]


def test_app_with_integrations_in_a_csv_cell() -> None:
    record = normalize_record(
        CIType.APP,
        {
            "name": "Slack",
            "app_type": "SaaS",
            "reported_usage_count": "52",
            "integrations": "GitHub; Jira",
        },
    )

    assert record.values == {
        "name": "Slack",
        "app_type": AppType.SAAS,
        "reported_usage_count": 52,
    }
    assert identifiers(record) == {(IdentifierKind.ALIAS, "slack")}
    assert [app.values["name"] for app in record.relations["integrations"]] == ["GitHub", "Jira"]


def test_unrecognized_values_are_collected_not_raised() -> None:
    record = normalize_record(
        CIType.DEVICE, {"hostname": "laptop-a", "status": "In Repair", "os": "Plan 9"}
    )

    assert record.unrecognized == [("status", "In Repair"), ("os", "Plan 9")]
    assert record.values == {"hostname": "laptop-a", "name": "laptop-a"}


def test_unknown_fields_are_kept_separately() -> None:
    record = normalize_record(CIType.DEVICE, {"hostname": "laptop-a", "cpu": "Apple M2 Pro"})

    assert record.unknown_fields == {"cpu": "Apple M2 Pro"}
    assert "cpu" not in record.values


def test_a_device_cannot_have_two_owners() -> None:
    record = normalize_record(CIType.DEVICE, {"hostname": "laptop-a", "owner": ["Jane", "John"]})

    assert "owner" not in record.relations
    assert record.unrecognized == [("owner", ["Jane", "John"])]


def test_missing_relation_values_are_ignored() -> None:
    record = normalize_record(CIType.USER, {"name": "Carlos S.", "apps": [], "email": None})

    assert record.relations == {}
    assert record.identifiers == []
