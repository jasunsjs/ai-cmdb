from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, SecretStr

from cmdb.ai.client import LLMResponseError, OpenRouterClient, build_llm_client
from cmdb.config import Settings


class Answer(BaseModel):
    value: int


class StubbedClient(OpenRouterClient):
    """OpenRouterClient with the HTTP call replaced by scripted replies."""

    def __init__(self, replies: list[str], **overrides: Any) -> None:
        super().__init__(
            Settings(_env_file=None, openrouter_api_key=SecretStr("test"), **overrides)
        )
        self.replies = list(replies)
        self.sent: list[list[dict[str, str]]] = []

    def _chat(self, messages: list[dict[str, str]], schema: type[BaseModel]) -> str:
        self.sent.append([dict(m) for m in messages])
        return self.replies.pop(0)


def ask(client: OpenRouterClient) -> Answer:
    return client.complete_json(system="sys", user="2+2?", schema=Answer, purpose="test")


def test_clean_json_reply() -> None:
    assert ask(StubbedClient(['{"value": 4}'])) == Answer(value=4)


def test_code_fenced_reply() -> None:
    assert ask(StubbedClient(['```json\n{"value": 4}\n```'])) == Answer(value=4)


def test_system_prompt_includes_the_json_schema() -> None:
    client = StubbedClient(['{"value": 4}'])
    ask(client)

    system = client.sent[0][0]["content"]
    assert system.startswith("sys")
    assert '"value"' in system


@pytest.mark.parametrize("bad_reply", ["not json", '{"value": "four"}', '{"other": 1}'])
def test_invalid_reply_is_repaired_once(bad_reply: str) -> None:
    client = StubbedClient([bad_reply, '{"value": 4}'])

    assert ask(client) == Answer(value=4)
    repair_messages = client.sent[1]
    assert repair_messages[-2] == {"role": "assistant", "content": bad_reply}
    assert "That reply was invalid" in repair_messages[-1]["content"]


def test_invalid_twice_raises() -> None:
    with pytest.raises(LLMResponseError):
        ask(StubbedClient(["nope", "still nope"]))


def fake_completion(content: str) -> SimpleNamespace:
    message = SimpleNamespace(content=content)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)


@pytest.mark.parametrize("structured", [True, False])
def test_response_format_follows_the_setting(structured: bool) -> None:
    client = OpenRouterClient(
        Settings(
            _env_file=None, openrouter_api_key=SecretStr("test"), llm_structured_outputs=structured
        )
    )
    captured: dict[str, Any] = {}

    def create(**kwargs: Any) -> SimpleNamespace:
        captured.update(kwargs)
        return fake_completion('{"value": 4}')

    client._client.chat.completions.create = create  # type: ignore[method-assign]

    assert ask(client) == Answer(value=4)
    assert captured["temperature"] == 0
    assert ("response_format" in captured) is structured


def test_no_api_key_means_no_client() -> None:
    assert build_llm_client(Settings(_env_file=None)) is None
    assert build_llm_client(Settings(_env_file=None, openrouter_api_key="  ")) is None
