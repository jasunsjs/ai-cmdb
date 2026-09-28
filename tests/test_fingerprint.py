from pathlib import Path

from cmdb.ingest.fingerprint import fingerprint, key_paths
from cmdb.ingest.records import load_file

INPUT_DATA = Path(__file__).parents[1] / "input_data"


def test_same_shape_with_different_values_matches() -> None:
    week_1 = [{"hostname": "laptop-a", "ip": "10.0.0.1"}]
    week_2 = [{"hostname": "laptop-b", "ip": "10.0.0.2"}, {"hostname": "laptop-c", "ip": None}]

    assert fingerprint("", week_1) == fingerprint("", week_2)


def test_added_key_changes_the_fingerprint() -> None:
    assert fingerprint("", [{"a": 1}]) != fingerprint("", [{"a": 1, "b": 2}])


def test_key_order_does_not_matter() -> None:
    assert fingerprint("", [{"a": 1, "b": 2}]) == fingerprint("", [{"b": 2, "a": 1}])


def test_record_path_matters() -> None:
    assert fingerprint("devices", [{"a": 1}]) != fingerprint("users", [{"a": 1}])


def test_sparse_records_use_the_union_of_keys() -> None:
    assert fingerprint("", [{"a": 1}, {"b": 2}]) == fingerprint("", [{"a": 1, "b": 2}])


def test_file_format_does_not_matter() -> None:
    from_csv = load_file("x.csv", b"hostname,ip\nlaptop-a,10.0.0.1\n")
    from_json = load_file("x.json", b'[{"hostname": "laptop-b", "ip": "10.0.0.2"}]')

    assert fingerprint(from_csv.record_path, from_csv.records) == fingerprint(
        from_json.record_path, from_json.records
    )


def test_nested_keys_are_flattened() -> None:
    parsed = load_file("x.yaml", (INPUT_DATA / "sample_hardware.yaml").read_bytes())

    paths = key_paths(parsed.records)

    assert "network.primary_ip" in paths
    assert "assigned_to.email" in paths
    assert "network" not in paths


def test_all_sample_files_have_distinct_fingerprints() -> None:
    fingerprints = set()
    for path in INPUT_DATA.iterdir():
        parsed = load_file(path.name, path.read_bytes())
        fingerprints.add(fingerprint(parsed.record_path, parsed.records))

    assert len(fingerprints) == 5
