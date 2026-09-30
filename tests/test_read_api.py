from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from cmdb.config import Settings
from cmdb.main import create_app
from fakes import SAMPLE_FILES, fake_ingest_ai, sample_bytes


@pytest.fixture
def api(settings: Settings) -> Iterator[TestClient]:
    """The app after uploading every sample file through POST /ingest."""
    with TestClient(create_app(settings, ingest_ai=fake_ingest_ai(), ask_ai=None)) as client:
        for filename in SAMPLE_FILES:
            response = client.post("/ingest", files={"file": (filename, sample_bytes(filename))})
            assert response.status_code == 200, response.json()
        yield client


def test_list_cis_with_filters(api: TestClient) -> None:
    devices = api.get("/cis", params={"type": "device"}).json()
    suspended = api.get("/cis", params={"status": "suspended"}).json()
    johns = api.get("/cis", params={"type": "user", "q": "john"}).json()

    assert sorted(d["name"] for d in devices) == ["laptop-devops", "laptop-jdoe", "macbook-cto-01"]
    assert [u["name"] for u in suspended] == ["Alex Johnson"]
    assert sorted(u["name"] for u in johns) == ["Alex Johnson", "John Doe"]


def test_pagination(api: TestClient) -> None:
    first = api.get("/cis", params={"type": "app", "limit": 5}).json()
    second = api.get("/cis", params={"type": "app", "limit": 5, "offset": 5}).json()

    assert len(first) == len(second) == 5
    assert {a["id"] for a in first}.isdisjoint(a["id"] for a in second)


def test_ci_detail(api: TestClient) -> None:
    [john] = api.get("/cis", params={"type": "user", "q": "john doe"}).json()

    detail = api.get(f"/cis/{john['id']}").json()

    assert detail["fields"]["email"] == "john.doe@example.com"
    assert {i["value"] for i in detail["identifiers"]} >= {
        "john.d@example.com",
        "john.doe@example.com",
        "emp-2156",
    }
    owns = [r for r in detail["relationships"] if r["rel_type"] == "OWNS"]
    assert [(r["direction"], r["other_name"]) for r in owns] == [("outgoing", "laptop-jdoe")]
    uses = {r["other_name"] for r in detail["relationships"] if r["rel_type"] == "USES"}
    assert uses == {"GitHub Enterprise", "Slack"}
    assert detail["field_provenance"]["email"]["observed_at"].startswith("2024-07-23")
    assert detail["source_records"]  # the Okta row that created the user


def test_device_detail_shows_its_owner(api: TestClient) -> None:
    [laptop] = api.get("/cis", params={"q": "laptop-jdoe"}).json()

    detail = api.get(f"/cis/{laptop['id']}").json()

    assert detail["fields"]["location"] == "London"
    assert detail["attributes"]["hardware.cpu"] == "Apple M2 Pro"
    owner = [r for r in detail["relationships"] if r["rel_type"] == "OWNS"]
    assert [(r["direction"], r["other_name"]) for r in owner] == [("incoming", "John Doe")]
    assert len(detail["source_records"]) == 3  # one per hardware file


def test_missing_ci_is_a_404(api: TestClient) -> None:
    assert api.get("/cis/99999").status_code == 404


def test_ingestions(api: TestClient) -> None:
    runs = api.get("/ingestions").json()
    detail = api.get(f"/ingestions/{runs[-1]['id']}").json()

    assert [r["filename"] for r in reversed(runs)] == SAMPLE_FILES
    assert all(r["status"] == "completed" for r in runs)
    assert detail["report"]["records"] == 9
    assert api.get("/ingestions/99999").status_code == 404


def test_mappings(api: TestClient) -> None:
    mappings = api.get("/mappings").json()
    first = api.get(f"/mappings/{mappings[0]['fingerprint']}").json()

    assert len(mappings) == 5
    assert first["created_by"] == "llm"
    assert first["spec"]["ci_type"] == "user"
    assert api.get("/mappings/nope").status_code == 404
