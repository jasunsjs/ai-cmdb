"""Tests against the real OpenRouter API. Run with `uv run pytest -m live`.

They read OPENROUTER_API_KEY from the environment or `.env` and are skipped without it.
LLM output varies, so they assert the facts that matter rather than exact wording.
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from cmdb.ai import build_ingest_ai
from cmdb.ai.client import build_llm_client
from cmdb.config import Settings
from cmdb.ingest.fingerprint import key_paths
from cmdb.ingest.mapping import build_context, validate_mapping
from cmdb.ingest.records import load_file
from cmdb.main import create_app
from fakes import SAMPLE_FILES, sample_bytes

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def live_settings(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    settings = Settings()
    if settings.openrouter_api_key is None:
        pytest.skip("OPENROUTER_API_KEY is not configured")
    database = tmp_path_factory.mktemp("live") / "cmdb.db"
    return settings.model_copy(update={"database_url": f"sqlite:///{database.as_posix()}"})


@pytest.fixture(scope="module")
def live_client(live_settings: Settings) -> Iterator[TestClient]:
    """The real app, with every sample file ingested through the real LLM."""
    with TestClient(create_app(live_settings)) as client:
        for filename in SAMPLE_FILES:
            response = client.post("/ingest", files={"file": (filename, sample_bytes(filename))})
            assert response.status_code == 200, response.json()
        yield client


class Sum(BaseModel):
    result: int


def test_round_trip(live_settings: Settings) -> None:
    llm = build_llm_client(live_settings)
    assert llm is not None

    reply = llm.complete_json(
        system="You are a calculator.", user="What is 2 + 3?", schema=Sum, purpose="live_test"
    )

    assert reply.result == 5


@pytest.mark.parametrize("filename", SAMPLE_FILES)
def test_generated_mappings_are_valid(live_settings: Settings, filename: str) -> None:
    ai = build_ingest_ai(build_llm_client(live_settings))
    parsed = load_file(filename, sample_bytes(filename))
    context = build_context(filename, parsed.record_path, parsed.records, parsed.metadata)

    spec = ai.mapper.generate(context)

    assert validate_mapping(spec, key_paths(parsed.records)) == []


def test_ingestion_builds_the_expected_cmdb(live_client: TestClient) -> None:
    def names(ci_type: str) -> list[str]:
        return [c["name"] for c in live_client.get("/cis", params={"type": ci_type}).json()]

    assert sorted(names("device")) == ["laptop-devops", "laptop-jdoe", "macbook-cto-01"]
    assert len(names("user")) == 9
    assert "GitHub Enterprise" in names("app")
    assert "GitHub" not in names("app")  # the Okta "GitHub" merged into the inventory record


def ask(client: TestClient, question: str) -> dict:
    response = client.post("/ask", json={"question": question})
    assert response.status_code == 200, response.json()
    return response.json()


def test_ask_users_without_mfa(live_client: TestClient) -> None:
    body = ask(live_client, "Which users don't have MFA?")

    emails = {value for row in body["rows"] for value in row.values() if "@" in str(value)}
    assert emails == {
        "c.santos@example.com",
        "m.brown@example.com",
        "a.johnson.contractor@example.com",
    }


def test_ask_device_owner(live_client: TestClient) -> None:
    body = ask(live_client, "Who owns laptop-jdoe?")

    assert "John Doe" in str(body["rows"])


def test_ask_integrations(live_client: TestClient) -> None:
    body = ask(live_client, "Which apps integrate with Slack?")

    assert "GitHub Enterprise" in str(body["rows"])


def test_ask_off_topic(live_client: TestClient) -> None:
    body = ask(live_client, "What's the weather in London today?")

    assert body["sql"] is None
    assert body["answer"]
