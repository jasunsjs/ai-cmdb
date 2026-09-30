from typing import Any

import pytest
from sqlalchemy.orm import Session

from cmdb.errors import AIStepError
from cmdb.ingest.canonical import normalize_record
from cmdb.ingest.report import RunNotes
from cmdb.ingest.resolver import (
    EntitySummary,
    MatchDecision,
    Resolver,
    abbreviation_compatible,
    name_key,
)
from cmdb.models import App, CIIdentifier, CIType, Device, IdentifierKind, User
from fakes import ScriptedJudge


def add_ci(session: Session, ci: Any, *identifiers: tuple[IdentifierKind, str, str]) -> Any:
    session.add(ci)
    session.flush()
    for kind, source_system, value in identifiers:
        session.add(CIIdentifier(ci_id=ci.id, kind=kind, source_system=source_system, value=value))
    session.flush()
    return ci


def make_resolver(session: Session, judge: Any = None, **overrides: Any) -> Resolver:
    options = {
        "judge": judge,
        "threshold": 0.8,
        "source_system": "okta",
        "filename": "test.json",
        "notes": RunNotes(),
    }
    return Resolver(session, **{**options, **overrides})


def user(**values: Any) -> Any:
    return normalize_record(CIType.USER, values)


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("john d", "john doe", True),
        ("j doe", "john doe", True),
        ("john doe", "john doe", True),
        ("jane doe", "john doe", False),
        ("john", "john doe", False),
        ("john d", "jane doe", False),
    ],
)
def test_abbreviation_compatible(a: str, b: str, expected: bool) -> None:
    assert abbreviation_compatible(a, b) is expected


def test_name_key() -> None:
    assert name_key("  John   D. ") == "john d"
    assert name_key("Jenkins CI/CD") == "jenkins ci cd"


def test_identifier_match(session: Session) -> None:
    john = add_ci(session, User(name="John Doe"), (IdentifierKind.EMAIL, "", "john@example.com"))

    result = make_resolver(session).resolve(user(name="Johnny", email="john@example.com"))

    assert (result.ci, result.method, result.confidence) == (john, "identifier", 1.0)


def test_identifiers_pointing_at_different_cis_use_the_strongest(session: Session) -> None:
    add_ci(session, User(name="A"), (IdentifierKind.EMAIL, "", "a@example.com"))
    b = add_ci(session, User(name="B"), (IdentifierKind.EMPLOYEE_ID, "", "emp-1"))
    resolver = make_resolver(session)

    result = resolver.resolve(user(name="X", email="a@example.com", employee_id="EMP-1"))

    assert result.ci == b  # employee_id outranks email
    assert "point at different CIs" in resolver.notes.warnings[0]


def test_external_ids_are_scoped_to_their_system(session: Session) -> None:
    add_ci(session, User(name="Jane"), (IdentifierKind.EXTERNAL_ID, "jira", "u_1"))

    result = make_resolver(session, source_system="okta").resolve(
        user(name="Someone Else", external_id="u_1")
    )

    assert result.method == "new"


def test_devices_are_never_fuzzy_matched(session: Session) -> None:
    add_ci(session, Device(name="laptop-a", hostname="laptop-a"))
    judge = ScriptedJudge()

    result = make_resolver(session, judge).resolve(
        normalize_record(CIType.DEVICE, {"name": "laptop-a", "location": "London"})
    )

    assert result.method == "new"
    assert judge.calls == []


def test_owner_context_links_an_abbreviated_name(session: Session) -> None:
    john = add_ci(session, User(name="John Doe"), (IdentifierKind.EMAIL, "", "john@example.com"))
    judge = ScriptedJudge()

    result = make_resolver(session, judge).resolve(user(name="John D."), context_owner=john)

    assert (result.ci, result.method) == (john, "context")
    assert judge.calls == []


def test_name_only_record_with_one_exact_candidate(session: Session) -> None:
    sarah = add_ci(session, User(name="Sarah Chen"), (IdentifierKind.EMAIL, "", "s@example.com"))
    judge = ScriptedJudge()

    result = make_resolver(session, judge).resolve(user(name="Sarah Chen"))

    assert (result.ci, result.method, result.confidence) == (sarah, "name_exact", 0.9)
    assert judge.calls == []


def test_same_name_with_a_different_email_asks_the_judge(session: Session) -> None:
    john = add_ci(session, User(name="John Doe"), (IdentifierKind.EMAIL, "", "john.d@example.com"))
    judge = ScriptedJudge()
    resolver = make_resolver(session, judge)

    result = resolver.resolve(user(name="John Doe", email="john.doe@example.com"))

    assert (result.ci, result.method, result.confidence) == (john, "llm", 0.9)
    assert len(judge.calls) == 1
    decision = resolver.notes.ai_decisions[0]
    assert decision["type"] == "entity_match"
    assert decision["accepted"] is True


def test_low_confidence_keeps_records_separate(session: Session) -> None:
    add_ci(session, User(name="John Doe"), (IdentifierKind.EMAIL, "", "john.d@example.com"))
    resolver = make_resolver(session, ScriptedJudge(confidence=0.5))

    result = resolver.resolve(user(name="John Doe", email="john.doe@example.com"))

    assert result.method == "new"
    assert resolver.notes.ai_decisions[0]["accepted"] is False
    assert "possible duplicate of 'John Doe'" in resolver.notes.warnings[0]


def test_different_employee_ids_veto_a_merge(session: Session) -> None:
    add_ci(session, User(name="John Doe"), (IdentifierKind.EMPLOYEE_ID, "", "emp-1"))
    judge = ScriptedJudge()

    result = make_resolver(session, judge).resolve(user(name="John Doe", employee_id="EMP-2"))

    assert result.method == "new"
    assert judge.calls == []


def test_different_ids_from_the_same_system_veto_a_merge(session: Session) -> None:
    add_ci(session, User(name="John Doe"), (IdentifierKind.EXTERNAL_ID, "okta", "u_1"))
    judge = ScriptedJudge()

    result = make_resolver(session, judge).resolve(user(name="John Doe", external_id="u_2"))

    assert result.method == "new"
    assert judge.calls == []


def test_similar_app_names_go_to_the_judge(session: Session) -> None:
    github = add_ci(session, App(name="GitHub", is_stub=True), (IdentifierKind.ALIAS, "", "github"))
    judge = ScriptedJudge()

    result = make_resolver(session, judge).resolve(
        normalize_record(CIType.APP, {"name": "GitHub Enterprise"})
    )

    assert (result.ci, result.method) == (github, "llm")


def test_ambiguous_without_a_judge_stays_separate(session: Session) -> None:
    add_ci(session, App(name="GitHub"), (IdentifierKind.ALIAS, "", "github"))
    resolver = make_resolver(session, judge=None)

    result = resolver.resolve(normalize_record(CIType.APP, {"name": "GitHub Enterprise"}))

    assert result.method == "new"
    assert "AI unavailable" in resolver.notes.warnings[0]


def test_judge_failure_keeps_records_separate(session: Session) -> None:
    add_ci(session, App(name="GitHub"), (IdentifierKind.ALIAS, "", "github"))

    class FailingJudge:
        def judge(self, *args: Any, **kwargs: Any) -> MatchDecision:
            raise AIStepError("timeout")

    resolver = make_resolver(session, FailingJudge())
    result = resolver.resolve(normalize_record(CIType.APP, {"name": "GitHub Enterprise"}))

    assert result.method == "new"
    assert "timeout" in resolver.notes.warnings[0]


def test_judge_cannot_pick_a_non_candidate(session: Session) -> None:
    add_ci(session, App(name="GitHub"), (IdentifierKind.ALIAS, "", "github"))

    class RogueJudge:
        def judge(
            self, incoming: EntitySummary, candidates: list[EntitySummary], *, context: str
        ) -> MatchDecision:
            return MatchDecision(candidate_id=9999, confidence=1.0, reason="made up")

    result = make_resolver(session, RogueJudge()).resolve(
        normalize_record(CIType.APP, {"name": "GitHub Enterprise"})
    )

    assert result.method == "new"


def test_name_only_matches_are_remembered_within_a_run(session: Session) -> None:
    resolver = make_resolver(session)
    record = user(name="Zed Q.")
    zed = add_ci(session, User(name="Zed Q."))
    resolver.remember(zed, record, 0.9)

    result = resolver.resolve(user(name="Zed Q."))

    assert (result.ci, result.method, result.confidence) == (zed, "memo", 0.9)
