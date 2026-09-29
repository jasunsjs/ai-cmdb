"""LLM classification of values the deterministic normalizers couldn't interpret."""

import json
from typing import Any

from pydantic import BaseModel

from cmdb.ai.client import LLMClient, LLMError
from cmdb.ingest.classify import ClassificationItem
from cmdb.ingest.errors import AIStepError

SYSTEM_PROMPT = """\
You clean up values in an IT inventory (CMDB). Each item gives a field, what it means, the
answer format to use, and the raw value found in a source file. Translate each raw value into
the requested format. Answer null when the raw value doesn't clearly say; never guess.
"""


class Classification(BaseModel):
    id: int
    value: str | bool | None


class Classifications(BaseModel):
    classifications: list[Classification]


class LLMValueClassifier:
    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def classify(self, items: list[ClassificationItem]) -> dict[int, Any]:
        lines = [
            json.dumps(
                {
                    "id": item.id,
                    "ci_type": item.ci_type.value,
                    "field": item.field,
                    "meaning": item.description,
                    "answer_format": item.expected,
                    "raw_value": item.raw,
                },
                default=str,
            )
            for item in items
        ]
        try:
            reply = self.llm.complete_json(
                system=SYSTEM_PROMPT,
                user="Items:\n" + "\n".join(lines),
                schema=Classifications,
                purpose="value_classification",
            )
        except LLMError as exc:
            raise AIStepError(str(exc)) from exc
        known = {item.id for item in items}
        return {c.id: c.value for c in reply.classifications if c.id in known}
