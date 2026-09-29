"""A minimal JSON-returning LLM client for OpenRouter (OpenAI-compatible API).

Every call asks for a JSON object matching a Pydantic schema, validates the reply, and makes
one repair attempt when the reply doesn't validate.
"""

import json
import logging
import re
import time
from typing import Any, Protocol

import openai
from pydantic import BaseModel, ValidationError

from cmdb.config import Settings

logger = logging.getLogger(__name__)

_CODE_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


class LLMError(Exception):
    """Base class for LLM failures."""


class LLMUnavailableError(LLMError):
    """The LLM could not be reached (network, auth, rate limit) after retries."""


class LLMResponseError(LLMError):
    """The LLM's reply was not valid JSON for the requested schema, even after a repair."""


class LLMClient(Protocol):
    def complete_json[T: BaseModel](
        self, *, system: str, user: str, schema: type[T], purpose: str
    ) -> T: ...


def parse_json_reply[T: BaseModel](text: str, schema: type[T]) -> T:
    """Parse a model reply into `schema`, tolerating Markdown code fences.

    Raises ValueError (json) or ValidationError (pydantic) on bad input.
    """
    stripped = text.strip()
    if match := _CODE_FENCE.match(stripped):
        stripped = match.group(1)
    return schema.model_validate(json.loads(stripped))


def json_instructions(schema: type[BaseModel]) -> str:
    return (
        "Reply with only a JSON object (no prose, no code fences) that matches this JSON "
        f"schema:\n{json.dumps(schema.model_json_schema())}"
    )


class OpenRouterClient:
    def __init__(self, settings: Settings) -> None:
        if settings.openrouter_api_key is None:
            raise ValueError("OPENROUTER_API_KEY is not set")
        self.model = settings.openrouter_model
        self.structured_outputs = settings.llm_structured_outputs
        self._client = openai.OpenAI(
            base_url=settings.openrouter_base_url,
            api_key=settings.openrouter_api_key.get_secret_value(),
            timeout=settings.llm_timeout_seconds,
            max_retries=2,
            default_headers={"X-Title": "AI CMDB"},
        )

    def complete_json[T: BaseModel](
        self, *, system: str, user: str, schema: type[T], purpose: str
    ) -> T:
        messages: list[dict[str, str]] = [
            {"role": "system", "content": f"{system}\n\n{json_instructions(schema)}"},
            {"role": "user", "content": user},
        ]
        started = time.perf_counter()
        for attempt in (1, 2):
            reply = self._chat(messages, schema)
            try:
                result = parse_json_reply(reply, schema)
            except (ValueError, ValidationError) as exc:
                if attempt == 2:
                    self._log(purpose, started, attempt, ok=False)
                    raise LLMResponseError(f"Invalid {purpose} reply from the LLM: {exc}") from exc
                messages += [
                    {"role": "assistant", "content": reply},
                    {
                        "role": "user",
                        "content": f"That reply was invalid: {exc}\n"
                        "Reply with only the corrected JSON object.",
                    },
                ]
                continue
            self._log(purpose, started, attempt, ok=True)
            return result
        raise AssertionError("unreachable")

    def _chat(self, messages: list[dict[str, str]], schema: type[BaseModel]) -> str:
        """One chat completion request; returns the reply text."""
        kwargs: dict[str, Any] = {}
        if self.structured_outputs:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "schema": schema.model_json_schema(),
                    "strict": False,
                },
            }
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=messages,  # type: ignore[arg-type]
                temperature=0,
                max_tokens=4096,
                **kwargs,
            )
        except openai.APIError as exc:
            raise LLMUnavailableError(f"LLM request failed: {exc}") from exc
        if not response.choices:
            raise LLMResponseError("LLM returned no choices")
        usage = response.usage
        if usage is not None:
            logger.debug(
                "llm usage model=%s prompt_tokens=%s completion_tokens=%s",
                self.model,
                usage.prompt_tokens,
                usage.completion_tokens,
            )
        return response.choices[0].message.content or ""

    def _log(self, purpose: str, started: float, attempts: int, *, ok: bool) -> None:
        logger.info(
            "llm_call purpose=%s model=%s attempts=%d latency_ms=%d ok=%s",
            purpose,
            self.model,
            attempts,
            (time.perf_counter() - started) * 1000,
            ok,
        )


def build_llm_client(settings: Settings) -> LLMClient | None:
    """The configured LLM client, or None when no API key is set."""
    if settings.openrouter_api_key is None:
        return None
    return OpenRouterClient(settings)
