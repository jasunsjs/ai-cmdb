from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from cmdb.models import App, CIRelationship, Device, RelType, User


@pytest.fixture
def graph(session: Session) -> dict[str, Any]:
    """Jane owns laptop-a and uses Slack + GitHub; Bob uses Slack; Slack integrates with GitHub."""
    jane = User(name="Jane Doe", email="jane@example.com", mfa_enabled=True)
    bob = User(name="Bob Smith", email="bob@example.com", mfa_enabled=False)
    laptop = Device(name="laptop-a", hostname="laptop-a")
    spare = Device(name="spare-01", hostname="spare-01")
    slack = App(name="Slack", reported_usage_count=52)
    github = App(name="GitHub Enterprise")
    jira = App(name="Jira", is_stub=True)
    session.add_all([jane, bob, laptop, spare, slack, github, jira])
    session.add_all(
        [
            CIRelationship(source=jane, target=laptop, rel_type=RelType.OWNS),
            CIRelationship(source=jane, target=slack, rel_type=RelType.USES),
            CIRelationship(source=jane, target=github, rel_type=RelType.USES, confidence=0.9),
            CIRelationship(source=bob, target=slack, rel_type=RelType.USES),
            # Stored in one direction only, as sources report it.
            CIRelationship(source=slack, target=github, rel_type=RelType.INTEGRATES_WITH),
        ]
    )
    session.commit()
    return {"jane": jane, "bob": bob, "slack": slack, "github": github}


def rows(session: Session, sql: str) -> list[dict[str, Any]]:
    return [dict(r) for r in session.execute(text(sql)).mappings()]


def test_v_devices_includes_owner(session: Session, graph: dict[str, Any]) -> None:
    result = rows(session, "SELECT hostname, owner_name, owner_email FROM v_devices ORDER BY 1")

    assert result == [
        {"hostname": "laptop-a", "owner_name": "Jane Doe", "owner_email": "jane@example.com"},
        {"hostname": "spare-01", "owner_name": None, "owner_email": None},
    ]


def test_v_users_counts_apps_and_devices(session: Session, graph: dict[str, Any]) -> None:
    result = rows(
        session, "SELECT name, mfa_enabled, app_count, device_count FROM v_users ORDER BY name"
    )

    assert result == [
        {"name": "Bob Smith", "mfa_enabled": 0, "app_count": 1, "device_count": 0},
        {"name": "Jane Doe", "mfa_enabled": 1, "app_count": 2, "device_count": 1},
    ]


def test_v_apps_compares_reported_and_observed_usage(
    session: Session, graph: dict[str, Any]
) -> None:
    result = rows(
        session,
        "SELECT name, reported_usage_count, observed_user_count, is_stub FROM v_apps ORDER BY name",
    )

    assert result == [
        {"name": "GitHub Enterprise", "reported_usage_count": None, "observed_user_count": 1,
         "is_stub": 0},
        {"name": "Jira", "reported_usage_count": None, "observed_user_count": 0, "is_stub": 1},
        {"name": "Slack", "reported_usage_count": 52, "observed_user_count": 2, "is_stub": 0},
    ]  # fmt: skip


def test_v_user_apps_lists_each_usage(session: Session, graph: dict[str, Any]) -> None:
    result = rows(session, "SELECT user_name, app_name, confidence FROM v_user_apps ORDER BY 1, 2")

    assert result == [
        {"user_name": "Bob Smith", "app_name": "Slack", "confidence": 1.0},
        {"user_name": "Jane Doe", "app_name": "GitHub Enterprise", "confidence": 0.9},
        {"user_name": "Jane Doe", "app_name": "Slack", "confidence": 1.0},
    ]


def test_v_app_integrations_is_symmetric(session: Session, graph: dict[str, Any]) -> None:
    result = rows(
        session, "SELECT app_name, integrated_app_name FROM v_app_integrations ORDER BY 1"
    )

    assert result == [
        {"app_name": "GitHub Enterprise", "integrated_app_name": "Slack"},
        {"app_name": "Slack", "integrated_app_name": "GitHub Enterprise"},
    ]


def test_v_relationships_resolves_both_ends(session: Session, graph: dict[str, Any]) -> None:
    result = rows(
        session,
        "SELECT source_type, source_name, rel_type, target_type, target_name "
        "FROM v_relationships ORDER BY relationship_id",
    )

    assert len(result) == 5
    assert result[0] == {
        "source_type": "user",
        "source_name": "Jane Doe",
        "rel_type": "OWNS",
        "target_type": "device",
        "target_name": "laptop-a",
    }
