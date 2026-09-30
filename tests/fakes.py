"""Deterministic stand-ins for the LLM and the AI services, so tests never hit the network."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from cmdb.ai.client import LLMResponseError, parse_json_reply
from cmdb.ingest.bundled import bundled_mappings
from cmdb.ingest.classify import ClassificationItem
from cmdb.ingest.errors import MappingGenerationError
from cmdb.ingest.mapping import MappingContext, MappingSpec
from cmdb.ingest.pipeline import IngestAI
from cmdb.ingest.resolver import EntitySummary, MatchDecision, name_key

TESTS_DIR = Path(__file__).parent
INPUT_DATA = TESTS_DIR.parent / "input_data"
SAMPLE_FILES = [
    "sample_okta.json",
    "sample_app.json",
    "sample_hardware.csv",
    "sample_hardware.json",
    "sample_hardware.yaml",
]


def fixture_mapping(filename: str) -> MappingSpec:
    """The hand-written mapping for a sample file (the app's bundled mappings)."""
    name = filename.replace(".", "_")
    for bundled in bundled_mappings().values():
        if bundled.name == name:
            return bundled.spec
    raise FileNotFoundError(f"no bundled mapping for {filename}")


class FixtureMapper:
    """Returns the hand-written mapping for a sample file; counts calls."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def generate(self, context: MappingContext) -> MappingSpec:
        self.calls.append(context.filename)
        try:
            return fixture_mapping(Path(context.filename).name)
        except FileNotFoundError as exc:
            raise MappingGenerationError(f"no fixture for {context.filename}") from exc


class FakeClassifier:
    """Answers from a dict keyed by (field, raw value); records the items it was asked."""

    def __init__(self, answers: dict[tuple[str, Any], Any] | None = None) -> None:
        self.answers = answers or {}
        self.items: list[ClassificationItem] = []

    def classify(self, items: list[ClassificationItem]) -> dict[int, Any]:
        self.items.extend(items)
        return {item.id: self.answers.get((item.field, item.raw)) for item in items}


# Name pairs the scripted judge treats as the same entity (plus identical names).
SAME_ENTITY = {
    frozenset({"john d", "john doe"}),
    frozenset({"github", "github enterprise"}),
    frozenset({"salesforce", "salesforce crm"}),
}


class ScriptedJudge:
    """A deterministic, symmetric stand-in for the LLM entity judge."""

    def __init__(self, confidence: float = 0.9) -> None:
        self.confidence = confidence
        self.calls: list[tuple[EntitySummary, list[EntitySummary]]] = []

    def judge(
        self, incoming: EntitySummary, candidates: list[EntitySummary], *, context: str
    ) -> MatchDecision:
        self.calls.append((incoming, candidates))
        key = name_key(incoming.name or "")
        for candidate in candidates:
            other = name_key(candidate.name or "")
            if other == key or frozenset({key, other}) in SAME_ENTITY:
                return MatchDecision(candidate.id, self.confidence, "scripted match")
        return MatchDecision(None, 0.1, "scripted: no match")


def fake_ingest_ai(
    *, classifier: FakeClassifier | None = None, judge: ScriptedJudge | None = None
) -> IngestAI:
    return IngestAI(
        mapper=FixtureMapper(),
        classifier=classifier or FakeClassifier(),
        judge=judge or ScriptedJudge(),
    )


def sample_bytes(filename: str) -> bytes:
    return (INPUT_DATA / filename).read_bytes()


Reply = str | dict[str, Any] | BaseModel | Exception | Callable[[str, str], Any]


class FakeLLMClient:
    """An LLMClient with scripted replies per `purpose`, consumed in order.

    A reply can be a JSON string, a dict, a model instance, an exception to raise, or a
    callable taking (system, user) and returning one of those. Replies go through the same
    parsing as the real client.
    """

    def __init__(self, replies: dict[str, list[Reply]] | None = None) -> None:
        self.replies: dict[str, list[Reply]] = {k: list(v) for k, v in (replies or {}).items()}
        self.calls: list[dict[str, Any]] = []

    def add(self, purpose: str, *replies: Reply) -> None:
        self.replies.setdefault(purpose, []).extend(replies)

    def calls_for(self, purpose: str) -> list[dict[str, Any]]:
        return [call for call in self.calls if call["purpose"] == purpose]

    def complete_json[T: BaseModel](
        self, *, system: str, user: str, schema: type[T], purpose: str
    ) -> T:
        self.calls.append({"purpose": purpose, "system": system, "user": user})
        queue = self.replies.get(purpose)
        if not queue:
            raise AssertionError(f"FakeLLMClient has no scripted reply for {purpose!r}")
        reply: Any = queue.pop(0)
        if callable(reply) and not isinstance(reply, type):
            reply = reply(system, user)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, schema):
            return reply
        text = reply if isinstance(reply, str) else json.dumps(reply)
        try:
            return parse_json_reply(text, schema)
        except (ValueError, ValidationError) as exc:
            raise LLMResponseError(f"Invalid {purpose} reply: {exc}") from exc
