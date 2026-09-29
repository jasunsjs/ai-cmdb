"""LLM judgment for ambiguous entity matches (e.g. "John D." vs "John Doe")."""

import json
from dataclasses import asdict

from pydantic import BaseModel, Field

from cmdb.ai.client import LLMClient, LLMError
from cmdb.ingest.errors import AIStepError
from cmdb.ingest.resolver import EntitySummary, MatchDecision

SYSTEM_PROMPT = """\
You decide whether an incoming record from an IT inventory refers to one of several existing
configuration items (people or applications) in a CMDB.

Guidance:
- Names may be abbreviated ("John D." for "John Doe") or carry product suffixes
  ("GitHub" for "GitHub Enterprise").
- Different email addresses do not prove different people: one system may use a short form
  (j.doe@) and another the full name (john.doe@). Weigh department, team and groups.
- Similar names can still be different people or different products ("Salesforce" vs
  "Salesforce Marketing Cloud"). Conflicting details should lower your confidence.
- Return the matching candidate's id with a confidence from 0 to 1, or candidate_id null
  if none of them is the same entity. A wrong merge is worse than a missed one.

Confidence scale:
- 0.95 or more: clearly the same entity (e.g. an abbreviated name plus consistent details).
- 0.8 to 0.95: very likely the same, with nothing contradicting it.
- 0.5 to 0.8: plausible but uncertain.
- below 0.5: probably different.
"""


class Decision(BaseModel):
    candidate_id: int | None = Field(description="id of the matching candidate, or null")
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(description="One short sentence")


class LLMEntityJudge:
    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def judge(
        self, incoming: EntitySummary, candidates: list[EntitySummary], *, context: str
    ) -> MatchDecision:
        user = "\n".join(
            [
                context,
                "Incoming record:",
                json.dumps(_describe(incoming), default=str),
                "Candidates:",
                *(json.dumps(_describe(c), default=str) for c in candidates),
            ]
        )
        try:
            reply = self.llm.complete_json(
                system=SYSTEM_PROMPT, user=user, schema=Decision, purpose="entity_match"
            )
        except LLMError as exc:
            raise AIStepError(str(exc)) from exc
        ids = {c.id for c in candidates}
        candidate_id = reply.candidate_id if reply.candidate_id in ids else None
        return MatchDecision(candidate_id, reply.confidence, reply.reason)


def _describe(entity: EntitySummary) -> dict[str, object]:
    data = asdict(entity)
    data["ci_type"] = entity.ci_type.value
    data["identifiers"] = [f"{kind}: {value}" for kind, value in entity.identifiers]
    if entity.id is None:
        data.pop("id")
    return data
