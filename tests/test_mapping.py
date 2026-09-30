from typing import Any

import pytest
from sqlalchemy.orm import Session

from cmdb.ingest.bundled import bundled_mapping
from cmdb.ingest.fingerprint import fingerprint, key_paths
from cmdb.ingest.mapping import (
    MappingSpec,
    apply_mapping,
    build_context,
    get_cached_mapping,
    save_mapping,
    validate_mapping,
)
from cmdb.ingest.records import load_file
from cmdb.models import CIType, MappingSource
from fakes import SAMPLE_FILES, fixture_mapping, sample_bytes

PATHS = ["hostname", "assigned_to.name", "assigned_to.email", "apps", "user_id", "last_checkin"]


def device_spec(**overrides: Any) -> MappingSpec:
    return MappingSpec(**{"ci_type": "device", "fields": {"hostname": "hostname"}, **overrides})


def load(filename: str) -> Any:
    return load_file(filename, sample_bytes(filename))


@pytest.mark.parametrize("filename", SAMPLE_FILES)
def test_fixture_mappings_are_valid_for_the_sample_files(filename: str) -> None:
    parsed = load(filename)

    assert validate_mapping(fixture_mapping(filename), key_paths(parsed.records)) == []


@pytest.mark.parametrize("filename", SAMPLE_FILES)
def test_each_sample_file_has_a_bundled_mapping(filename: str) -> None:
    parsed = load(filename)

    # Fails if a sample file's structure (and so its fingerprint) drifts from the bundle.
    assert bundled_mapping(fingerprint(parsed.record_path, parsed.records)) == fixture_mapping(
        filename
    )


def test_minimal_valid_spec() -> None:
    assert validate_mapping(device_spec(), PATHS) == []


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        (device_spec(fields={"hostname": "hostname", "colour": "hostname"}), "not a device field"),
        (device_spec(fields={"hostname": "host"}), "does not exist"),
        (device_spec(fields={"ip_address": "hostname"}), "identifying field"),
        (
            device_spec(related=[{"relation": "apps", "fields": {"name": "apps"}}]),
            "not a device relation",
        ),
        (
            device_spec(related=[{"relation": "owner", "fields": {"shoe_size": "hostname"}}]),
            "not a user field",
        ),
        (device_spec(observed_at="checked_in"), "does not exist"),
        (
            MappingSpec(
                ci_type=CIType.USER,
                fields={"name": "assigned_to.name"},
                related=[{"relation": "apps", "fields": {"name": "apps", "vendor": "apps"}}],
            ),
            "exactly 'name'",
        ),
        (
            MappingSpec(
                ci_type=CIType.USER, fields={"name": "assigned_to.name", "external_id": "user_id"}
            ),
            "required when external_id",
        ),
        (device_spec(source_system="okta!"), "short slug"),
    ],
)
def test_validation_errors(spec: MappingSpec, message: str) -> None:
    errors = validate_mapping(spec, PATHS)

    assert any(message in error for error in errors), errors


def test_source_system_is_normalized_to_a_slug() -> None:
    assert device_spec(source_system="  Okta Prod ").source_system == "okta_prod"
    assert device_spec(source_system="   ").source_system is None


def test_unknown_mapping_keys_are_rejected() -> None:
    with pytest.raises(ValueError, match="Extra inputs"):
        MappingSpec.model_validate({"ci_type": "device", "fields": {}, "surprise": 1})


# --- applying ---


def test_apply_mapping_to_a_csv_row() -> None:
    parsed = load("sample_hardware.csv")

    mapped = apply_mapping(fixture_mapping("sample_hardware.csv"), parsed.records[0])

    assert mapped.values["hostname"] == "laptop-jdoe"
    assert mapped.values["encryption_enabled"] == "FileVault Enabled"  # values are not changed
    assert mapped.values["owner"] == {"name": "John D.", "department": "Engineering"}
    assert mapped.observed_at_raw == "2024-07-22T10:00:00Z"
    assert mapped.extras == {}  # every CSV column is mapped


def test_apply_mapping_to_a_nested_yaml_record() -> None:
    parsed = load("sample_hardware.yaml")

    mapped = apply_mapping(fixture_mapping("sample_hardware.yaml"), parsed.records[0])

    assert mapped.values["ip_address"] == "10.10.22.5"
    assert mapped.values["owner"]["email"] == "john.doe@example.com"
    assert mapped.values["owner"]["employee_id"] == "EMP-2156"
    assert mapped.extras["hardware.cpu"] == "Apple M2 Pro"
    assert "network.primary_ip" not in mapped.extras
    assert "assigned_to.email" not in mapped.extras


def test_apply_mapping_passes_list_relations_through() -> None:
    parsed = load("sample_okta.json")

    mapped = apply_mapping(fixture_mapping("sample_okta.json"), parsed.records[1])

    assert mapped.values["apps"] == ["GitHub", "Slack", "Salesforce"]
    assert mapped.values["external_id"] == "u_110"


def test_build_context_samples_distinct_values() -> None:
    records = [{"status": "active"}, {"status": "active"}, {"status": "inactive"}]

    context = build_context("x.json", "", records, {"export_date": "2024-01-01"})

    assert context.key_paths == ["status"]
    assert context.samples == {"status": ["active", "inactive"]}
    assert context.metadata == {"export_date": "2024-01-01"}


# --- cache ---


def test_mapping_cache_round_trip(session: Session) -> None:
    spec = fixture_mapping("sample_okta.json")

    save_mapping(
        session, fingerprint="abc", record_path="", spec=spec, created_by=MappingSource.LLM
    )
    session.commit()

    assert get_cached_mapping(session, "abc") == spec
    assert get_cached_mapping(session, "missing") is None


def test_concurrent_save_keeps_the_first_mapping(session: Session) -> None:
    first = fixture_mapping("sample_okta.json")
    second = fixture_mapping("sample_app.json")
    save_mapping(
        session, fingerprint="abc", record_path="", spec=first, created_by=MappingSource.LLM
    )

    kept = save_mapping(
        session, fingerprint="abc", record_path="", spec=second, created_by=MappingSource.LLM
    )

    assert kept == first
