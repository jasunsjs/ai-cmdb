from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from cmdb.config import Settings
from cmdb.main import create_app
from fakes import fake_ingest_ai, sample_bytes


@pytest.fixture
def ai_client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings, ingest_ai=fake_ingest_ai(), ask_ai=None)) as client:
        yield client


def upload(client: TestClient, filename: str, content: bytes | None = None, **data: str):
    content = sample_bytes(filename) if content is None else content
    return client.post("/ingest", files={"file": (filename, content)}, data=data)


def test_ingest_returns_a_report(ai_client: TestClient) -> None:
    response = upload(ai_client, "sample_okta.json")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["format"] == "json"
    assert body["ci_type"] == "user"
    assert body["mapping_source"] == "llm"
    assert body["records"] == 9
    assert body["cis"]["user"] == {"created": 9, "updated": 0, "unchanged": 0}
    assert body["relationships_created"] == {"USES": 25}
    assert body["ingestion_id"] >= 1


def test_source_system_form_field(ai_client: TestClient) -> None:
    response = upload(ai_client, "sample_okta.json", source_system="okta-eu")

    assert response.json()["source_system"] == "okta-eu"


def test_invalid_source_system_is_rejected(ai_client: TestClient) -> None:
    response = upload(ai_client, "sample_okta.json", source_system="okta; drop")

    assert response.status_code == 422


def test_unparseable_file_is_a_422_with_the_run_id(ai_client: TestClient) -> None:
    response = upload(ai_client, "broken.json", b'{"a": ')

    assert response.status_code == 422
    assert "Invalid JSON" in response.json()["detail"]
    assert response.json()["ingestion_id"] is not None


def test_new_format_without_ai_is_a_503(client: TestClient) -> None:
    response = upload(client, "racks.json", b'[{"asset": "srv-01", "rack": "B2"}]')

    assert response.status_code == 503
    assert "no LLM is configured" in response.json()["detail"]


def test_sample_file_without_ai_uses_the_bundled_mapping(client: TestClient) -> None:
    response = upload(client, "sample_okta.json")

    assert response.status_code == 200
    assert response.json()["mapping_source"] == "bundled"
    assert response.json()["cis"]["user"]["created"] == 9


def test_oversized_upload_is_a_413(settings: Settings) -> None:
    small = settings.model_copy(update={"max_upload_bytes": 10})
    with TestClient(create_app(small, ingest_ai=fake_ingest_ai(), ask_ai=None)) as client:
        response = upload(client, "sample_okta.json")

    assert response.status_code == 413
