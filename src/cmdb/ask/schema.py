"""The schema description and examples given to the SQL generator.

Columns are read from the database itself, so the description always matches the views;
the notes explain what the columns mean.
"""

import sqlite3

ALLOWED_VIEWS = (
    "v_devices",
    "v_users",
    "v_apps",
    "v_user_apps",
    "v_app_integrations",
    "v_relationships",
)

VIEW_NOTES = {
    "v_devices": "One row per device, with its owner (owner_* columns are NULL if unassigned).",
    "v_users": "One row per person. app_count = apps they use; device_count = devices they own.",
    "v_apps": (
        "One row per application. reported_usage_count = user count reported by the source "
        "inventory; observed_user_count = users actually linked to the app in the CMDB. "
        "is_stub = 1 when the app is only known by name (referenced, never inventoried)."
    ),
    "v_user_apps": "One row per (user, application) the user uses.",
    "v_app_integrations": (
        "One row per integration, listed in BOTH directions: app_name integrates with "
        "integrated_app_name."
    ),
    "v_relationships": (
        "Every relationship: OWNS (user -> device), USES (user -> app), "
        "INTEGRATES_WITH (app -> app)."
    ),
}

COLUMN_NOTES = {
    "status": "one of: active, inactive, suspended, unknown",
    "app_type": "saas or on_prem (NULL if unknown)",
    "encryption_enabled": "1 = yes, 0 = no, NULL = unknown",
    "mfa_enabled": "1 = yes, 0 = no, NULL = unknown",
    "sso_enabled": "1 = yes, 0 = no, NULL = unknown",
    "is_stub": "1 = only known by name",
    "groups": "JSON array text; search it with json_each(groups)",
    "last_seen": "UTC text 'YYYY-MM-DD HH:MM:SS.ffffff'",
    "last_login": "UTC text 'YYYY-MM-DD HH:MM:SS.ffffff'",
    "source_type": "device, user or app",
    "target_type": "device, user or app",
    "rel_type": "OWNS, USES or INTEGRATES_WITH",
    "confidence": "0..1 confidence of the identity match behind the relationship",
    "attributes": (
        "JSON object of extra source fields (keys listed below); read one with "
        "json_extract(attributes, '$.\"<key>\"'), always quoting the key because keys may "
        "contain dots. Values may be text, so CAST numbers before comparing or summing"
    ),
}

# Which view exposes each CI type's attributes.
ATTRIBUTE_VIEWS = {"device": "v_devices", "user": "v_users", "app": "v_apps"}
MAX_ATTRIBUTE_KEYS = 30
_MAX_EXAMPLE_CHARS = 40

EXAMPLES = [
    (
        "Which users don't have MFA?",
        "SELECT name, email FROM v_users WHERE mfa_enabled = 0 ORDER BY name",
    ),
    (
        "Who owns laptop-jdoe?",
        "SELECT hostname, owner_name, owner_email FROM v_devices "
        "WHERE hostname LIKE '%laptop-jdoe%'",
    ),
    (
        "Which apps integrate with Slack?",
        "SELECT DISTINCT integrated_app_name FROM v_app_integrations "
        "WHERE app_name LIKE '%slack%' ORDER BY integrated_app_name",
    ),
    (
        "Who is in the Engineering group?",
        "SELECT DISTINCT u.name, u.email FROM v_users AS u, json_each(u.groups) AS g "
        "WHERE g.value LIKE 'engineering' ORDER BY u.name",
    ),
    (
        "Which apps cost the most per year?",
        "SELECT name, CAST(json_extract(attributes, '$.\"annual_cost_usd\"') AS REAL) "
        "AS annual_cost_usd FROM v_apps WHERE annual_cost_usd IS NOT NULL "
        "ORDER BY annual_cost_usd DESC",
    ),
]


def describe_schema(connection: sqlite3.Connection) -> str:
    attribute_keys = _attribute_keys(connection)
    lines = []
    for view in ALLOWED_VIEWS:
        columns = [row[1] for row in connection.execute(f"PRAGMA table_info({view})")]
        lines.append(f"{view}: {VIEW_NOTES[view]}")
        for column in columns:
            note = COLUMN_NOTES.get(column)
            lines.append(f"  - {column}" + (f" ({note})" if note else ""))
        if keys := attribute_keys.get(view):
            lines.append("  attribute keys present (key: example value):")
            lines += [f"    - {key}: {example}" for key, example in keys]
    return "\n".join(lines)


def _attribute_keys(connection: sqlite3.Connection) -> dict[str, list[tuple[str, str]]]:
    """The attribute keys actually stored, per view, most common first, with an example."""
    rows = connection.execute(
        "SELECT ci.ci_type, j.key, COUNT(*) AS n, MIN(j.value) "
        "FROM configuration_items AS ci, json_each(ci.attributes) AS j "
        "GROUP BY ci.ci_type, j.key ORDER BY ci.ci_type, n DESC, j.key"
    )
    keys: dict[str, list[tuple[str, str]]] = {}
    for ci_type, key, _count, example in rows:
        view = ATTRIBUTE_VIEWS.get(ci_type)
        if view is None or len(keys.setdefault(view, [])) >= MAX_ATTRIBUTE_KEYS:
            continue
        text = str(example)
        if len(text) > _MAX_EXAMPLE_CHARS:
            text = text[: _MAX_EXAMPLE_CHARS - 3] + "..."
        keys[view].append((key, text))
    return keys


def describe_examples() -> str:
    return "\n".join(f"Q: {question}\nSQL: {sql}" for question, sql in EXAMPLES)
