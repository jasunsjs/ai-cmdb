"""The LLM-backed services, driven by a fake LLM client (no network)."""

import pytest

from cmdb.ai.classifier import LLMValueClassifier
from cmdb.ai.client import LLMUnavailableError
from cmdb.ai.mapper import LLMMappingGenerator, MappingReply
from cmdb.ai.matcher import LLMEntityJudge
from cmdb.ai.sql import LLMAnswerWriter, LLMSqlGenerator
from cmdb.ask.sql_guard import QueryResult
from cmdb.errors import AIStepError
from cmdb.ingest.canonical import normalize_record
from cmdb.ingest.classify import ClassificationItem, resolve_unrecognized
from cmdb.ingest.errors import MappingGenerationError
from cmdb.ingest.fingerprint import key_paths
from cmdb.ingest.mapping import MappingSpec, build_context
from cmdb.ingest.records import load_file
from cmdb.ingest.report import RunNotes
from cmdb.ingest.resolver import EntitySummary
from cmdb.models import CIStatus, CIType
from fakes import FakeLLMClient, fixture_mapping, sample_bytes


def as_reply(spec: MappingSpec) -> dict:
    """A MappingSpec in the list-of-pairs shape the LLM returns."""
    return {
        "ci_type": spec.ci_type.value,
        "fields": [{"field": f, "source": s} for f, s in spec.fields.items()],
        "related": [
            {
                "relation": r.relation,
                "fields": [{"field": f, "source": s} for f, s in r.fields.items()],
            }
            for r in spec.related
        ],
        "observed_at": spec.observed_at,
        "source_system": spec.source_system,
    }


def okta_context():
    parsed = load_file("sample_okta.json", sample_bytes("sample_okta.json"))
    context = build_context("sample_okta.json", parsed.record_path, parsed.records, {})
    assert context.key_paths == key_paths(parsed.records)
    return context


# --- mapping ---


def test_mapper_returns_a_valid_spec() -> None:
    expected = fixture_mapping("sample_okta.json")
    llm = FakeLLMClient({"schema_mapping": [as_reply(expected)]})

    spec = LLMMappingGenerator(llm).generate(okta_context())

    assert spec == expected
    call = llm.calls[0]
    assert "sample_okta.json" in call["user"]
    assert "mfa_enabled: true, false" in call["user"]  # key paths with sample values
    assert "Network hostname" in call["system"]  # canonical vocabulary descriptions
    assert "map any of its fields: name, email" in call["system"]  # related entity fields


def test_mapping_reply_converts_pairs_to_a_spec() -> None:
    expected = fixture_mapping("sample_hardware.yaml")

    assert MappingReply.model_validate(as_reply(expected)).to_spec() == expected


def test_mapper_repairs_an_invalid_spec_once() -> None:
    good = as_reply(fixture_mapping("sample_okta.json"))
    bad = {**good, "fields": [{"field": "name", "source": "full_name"}]}
    llm = FakeLLMClient({"schema_mapping": [bad, good]})

    spec = LLMMappingGenerator(llm).generate(okta_context())

    assert spec.fields["name"] == "name"
    retry_prompt = llm.calls[1]["user"]
    assert "previous mapping had these problems" in retry_prompt
    assert "'full_name' does not exist" in retry_prompt


def test_mapper_gives_up_after_the_repair() -> None:
    bad = {"ci_type": "user", "fields": [{"field": "name", "source": "nope"}]}
    llm = FakeLLMClient({"schema_mapping": [bad, bad]})

    with pytest.raises(MappingGenerationError, match="does not exist"):
        LLMMappingGenerator(llm).generate(okta_context())


def test_mapper_wraps_llm_errors() -> None:
    llm = FakeLLMClient({"schema_mapping": [LLMUnavailableError("down")]})

    with pytest.raises(MappingGenerationError, match="down"):
        LLMMappingGenerator(llm).generate(okta_context())


# --- value classification ---


def test_classifier_returns_answers_by_id() -> None:
    llm = FakeLLMClient(
        {"value_classification": [{"classifications": [
            {"id": 0, "value": "inactive"}, {"id": 99, "value": "ignored"}]}]}
    )  # fmt: skip
    item = ClassificationItem(0, CIType.DEVICE, "status", "Lifecycle status", "one of ...", "RMA")

    assert LLMValueClassifier(llm).classify([item]) == {0: "inactive"}
    assert '"raw_value": "RMA"' in llm.calls[0]["user"]


def test_classified_values_pass_through_the_normalizers() -> None:
    records = [
        normalize_record(CIType.DEVICE, {"hostname": "a", "status": "In Repair"}),
        normalize_record(CIType.DEVICE, {"hostname": "b", "status": "In Repair"}),
        normalize_record(CIType.DEVICE, {"hostname": "c", "status": "Lost"}),
    ]
    llm = FakeLLMClient(
        {"value_classification": [{"classifications": [
            {"id": 0, "value": "inactive"}, {"id": 1, "value": "gone fishing"}]}]}
    )  # fmt: skip
    notes = RunNotes()

    resolve_unrecognized(records, LLMValueClassifier(llm), notes)

    assert [r.values.get("status") for r in records] == [CIStatus.INACTIVE, CIStatus.INACTIVE, None]
    assert records[0].unrecognized == []
    assert records[2].unrecognized == [("status", "Lost")]  # the answer wasn't a valid status
    assert [d["accepted"] for d in notes.ai_decisions] == [True, False]
    assert len(llm.calls) == 1  # one batched call, duplicate values asked once


def test_classifier_failure_is_a_warning() -> None:
    records = [normalize_record(CIType.DEVICE, {"hostname": "a", "status": "In Repair"})]
    llm = FakeLLMClient({"value_classification": [LLMUnavailableError("down")]})
    notes = RunNotes()

    resolve_unrecognized(records, LLMValueClassifier(llm), notes)

    assert records[0].unrecognized == [("status", "In Repair")]
    assert "unavailable" in notes.warnings[0]


# --- entity matching ---

INCOMING = EntitySummary(None, CIType.USER, "John D.", [], {"department": "Engineering"})
CANDIDATES = [EntitySummary(1, CIType.USER, "John Doe", [("email", "john.d@example.com")])]


def test_judge_returns_the_decision() -> None:
    llm = FakeLLMClient(
        {"entity_match": [{"candidate_id": 1, "confidence": 0.93, "reason": "abbreviation"}]}
    )

    decision = LLMEntityJudge(llm).judge(INCOMING, CANDIDATES, context="from test.csv")

    assert (decision.candidate_id, decision.confidence) == (1, 0.93)
    assert "John D." in llm.calls[0]["user"]
    assert "email: john.d@example.com" in llm.calls[0]["user"]


def test_judge_ignores_ids_that_are_not_candidates() -> None:
    llm = FakeLLMClient({"entity_match": [{"candidate_id": 7, "confidence": 1, "reason": "?"}]})

    assert LLMEntityJudge(llm).judge(INCOMING, CANDIDATES, context="").candidate_id is None


def test_judge_wraps_llm_errors() -> None:
    llm = FakeLLMClient({"entity_match": [LLMUnavailableError("down")]})

    with pytest.raises(AIStepError):
        LLMEntityJudge(llm).judge(INCOMING, CANDIDATES, context="")


# --- /ask ---


def test_sql_generator_includes_the_schema_and_previous_error() -> None:
    llm = FakeLLMClient({"ask_sql": [{"sql": "SELECT 1", "clarification": None}]})

    plan = LLMSqlGenerator(llm).generate(
        "How many?", schema="v_users: ...", examples="Q: x", previous_sql="SELEC", error="syntax"
    )

    assert plan.sql == "SELECT 1"
    assert "v_users: ..." in llm.calls[0]["system"]
    assert "Error: syntax" in llm.calls[0]["user"]


def test_answer_writer_sends_the_rows() -> None:
    llm = FakeLLMClient({"ask_answer": [{"answer": "Two users."}]})
    result = QueryResult(columns=["name"], rows=[["Ann"], ["Bob"]], truncated=True)

    answer = LLMAnswerWriter(llm).write("Who?", sql="SELECT name FROM v_users", result=result)

    assert answer == "Two users."
    assert '{"name": "Ann"}' in llm.calls[0]["user"]
    assert "results truncated" in llm.calls[0]["user"]
