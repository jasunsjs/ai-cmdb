from pathlib import Path

import pytest

from cmdb.ingest.errors import ParseError
from cmdb.ingest.parsers import Format, decode, detect_format
from cmdb.ingest.records import find_records, flatten, load_file

INPUT_DATA = Path(__file__).parents[1] / "input_data"


def sample(name: str) -> bytes:
    return (INPUT_DATA / name).read_bytes()


# --- the provided sample files ---


@pytest.mark.parametrize(
    ("filename", "fmt", "record_path", "count"),
    [
        ("sample_app.json", Format.JSON, "applications", 6),
        ("sample_hardware.csv", Format.CSV, "", 3),
        ("sample_hardware.json", Format.JSON, "", 3),
        ("sample_hardware.yaml", Format.YAML, "inventory.devices", 3),
        ("sample_okta.json", Format.JSON, "", 9),
    ],
)
def test_sample_files_load(filename: str, fmt: Format, record_path: str, count: int) -> None:
    parsed = load_file(filename, sample(filename))

    assert parsed.format is fmt
    assert parsed.record_path == record_path
    assert len(parsed.records) == count


@pytest.mark.parametrize(
    "filename",
    [
        "sample_app.json",
        "sample_hardware.csv",
        "sample_hardware.json",
        "sample_hardware.yaml",
        "sample_okta.json",
    ],
)
def test_format_is_sniffed_without_an_extension(filename: str) -> None:
    expected = load_file(filename, sample(filename)).format

    assert load_file("upload", sample(filename)).format is expected


def test_metadata_holds_the_rest_of_the_document() -> None:
    parsed = load_file("sample_app.json", sample("sample_app.json"))

    assert parsed.metadata["inventory_metadata.export_date"] == "2024-07-21T00:00:00Z"
    assert parsed.metadata["summary.total_apps"] == 6
    assert "applications" not in parsed.metadata


def test_yaml_values_stay_text() -> None:
    parsed = load_file("sample_hardware.yaml", sample("sample_hardware.yaml"))
    laptop = parsed.records[0]

    assert parsed.metadata["inventory.metadata.export_date"] == "2024-07-23T14:00:00Z"
    assert laptop["hardware"]["ram_gb"] == "32"
    assert laptop["security"]["encryption_enabled"] == "true"


def test_yaml_does_not_coerce_lookalike_values() -> None:
    parsed = load_file("x.yaml", b"- version: 14.10\n  country: NO\n  seen: 2024-07-15\n")

    assert parsed.records == [{"version": "14.10", "country": "NO", "seen": "2024-07-15"}]


def test_csv_values_are_trimmed_and_empty_cells_become_none() -> None:
    parsed = load_file("x.csv", b" hostname , ip \n laptop-a ,\n")

    assert parsed.records == [{"hostname": "laptop-a", "ip": None}]


def test_csv_byte_order_mark_is_dropped() -> None:
    parsed = load_file("x.csv", b"\xef\xbb\xbfhostname,ip\nlaptop-a,10.0.0.1\n")

    assert list(parsed.records[0]) == ["hostname", "ip"]


def test_csv_blank_rows_are_skipped() -> None:
    parsed = load_file("x.csv", b"hostname,ip\nlaptop-a,10.0.0.1\n,\n")

    assert len(parsed.records) == 1


# --- record discovery ---


def test_largest_record_list_wins() -> None:
    record_set = find_records({"admins": [{"id": 1}], "users": [{"id": 1}, {"id": 2}]})

    assert record_set.record_path == "users"


def test_equal_sized_record_lists_prefer_the_shallowest() -> None:
    record_set = find_records({"a": {"b": [{"id": 1}]}, "c": [{"id": 2}]})

    assert record_set.record_path == "c"


def test_flatten_uses_dotted_keys_and_keeps_lists() -> None:
    record = {"network": {"primary_ip": "10.0.0.1", "wifi": {"on": "true"}}, "apps": ["Slack"]}

    assert flatten(record) == {
        "network.primary_ip": "10.0.0.1",
        "network.wifi.on": "true",
        "apps": ["Slack"],
    }


# --- errors ---


@pytest.mark.parametrize(
    ("filename", "content", "message"),
    [
        ("x.json", b"", "empty"),
        ("x.json", b"  \n\t", "empty"),
        ("x.json", b"\xff\xfe\x00", "UTF-8"),
        ("x.json", b'{"a": ', "Invalid JSON"),
        ("x.json", b'{"a": 1, "b": {"c": 2}}', "No list of records"),
        ("x.json", b"[]", "No records"),
        ("x.json", b"[1, 2]", "must be an object"),
        ("x.json", b'"just a string"', "must be a list of records"),
        ("x.yaml", b"just some text", "mapping or a list"),
        ("x.yaml", b"key: [unclosed", "Invalid YAML"),
        ("x.csv", b"hostname,ip\nlaptop-a,10.0.0.1,extra\n", "more values than the header"),
        ("upload", b"just some text", "Could not detect"),
    ],
)
def test_bad_files_raise_parse_error(filename: str, content: bytes, message: str) -> None:
    with pytest.raises(ParseError, match=message):
        load_file(filename, content)


def test_extension_takes_priority_over_content() -> None:
    assert detect_format("data.csv", decode(b'[{"a": 1}]')) is Format.CSV
