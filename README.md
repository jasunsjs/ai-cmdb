# AI CMDB

An AI-first Configuration Management Database (CMDB) backend. It ingests raw IT and SaaS inventory exports (hardware lists, Okta user dumps, app inventories) in JSON, YAML or CSV. It normalizes them into **configuration items** (devices, users, apps) and the **relationships** between them, and answers natural-language questions about the result, such as *"Which users don't have MFA?"*

Real inventory data is messy. The same laptop shows up in three exports with three different shapes. One file says the owner is `John D.`, another says `john.doe@example.com`. OS strings range from `macos` to `macOS Sonoma 14.5`. This service reconciles all of that into one queryable model. Deterministic rules do the work wherever possible, and an LLM is used only where rules fall short.

## Status

In active development. See [`docs/ROADMAP.md`](docs/ROADMAP.md) for the full plan and the design decisions.

| Area | State |
|---|---|
| Data model, database, SQL views | Done |
| Parsing, fingerprinting, value normalization | Done |
| LLM client (OpenRouter) | Planned |
| `POST /ingest` pipeline | Planned |
| `POST /ask` natural-language queries | Planned |
| Read endpoints and demo script | Planned |

## How it works

```
upload → parse → fingerprint → get mapping → apply mapping → normalize → resolve → store
```

1. **Parse.** The file format is detected and the list of records located, whether it's a root array, `applications`, or `inventory.devices`.
2. **Fingerprint.** The *structure* of the records (their field names) is hashed. Every export with the same structure gets the same fingerprint.
3. **Map.** A mapping translates the source's field names into the canonical vocabulary, for example `network.primary_ip → ip_address`. Mappings are cached per fingerprint, so **the LLM writes a mapping once per new format**, never per record.
4. **Normalize.** Rule-based normalizers clean each value (`"FileVault Enabled"` → `true`, `"Ubuntu 22.04 LTS"` → `Ubuntu` / `22.04`). Only values the rules can't classify go to the LLM, in a single batched call.
5. **Resolve.** Records are matched to existing CIs by strong identifiers (email, serial, asset tag). The LLM judges only ambiguous cases like `John D.` vs `John Doe`, and every decision is recorded with a confidence and a reason.
6. **Ask.** Questions are translated into read-only SQL over curated views. The generated SQL is returned alongside the answer.

## Getting started

The only prerequisite is [uv](https://docs.astral.sh/uv/getting-started/installation/). uv installs the pinned Python version and all dependencies itself.

```bash
cp .env.example .env      # Windows: copy .env.example .env
                          # then set OPENROUTER_API_KEY in .env
uv run cmdb-serve         # starts the API on http://127.0.0.1:8000 (docs at /docs)
uv run pytest             # runs the test suite (no API key needed)
```

Run commands from the repository root. The SQLite database is created at `data/cmdb.db` on first start.

### Configuration

Settings come from environment variables or `.env`. Only the API key is required. The defaults live in `src/cmdb/config.py`.

| Variable | Default | Purpose |
|---|---|---|
| `OPENROUTER_API_KEY` | none | API key for LLM features |
| `OPENROUTER_MODEL` | `anthropic/claude-haiku-4.5` | Model used for all LLM calls |
| `DATABASE_URL` | `sqlite:///data/cmdb.db` | SQLAlchemy database URL |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Server address |

## Tech stack

Python 3.13, FastAPI, SQLAlchemy 2 on SQLite, pydantic-settings, PyYAML, pytest and ruff, all managed with uv. LLM access goes through [OpenRouter](https://openrouter.ai).

## Project layout

```
src/cmdb/
  main.py          app factory and `cmdb-serve` entry point
  config.py        settings
  db.py            engine, SQLite setup, schema initialization
  models/          ORM models: CIs, relationships, identifiers, ingestion history
  sql/views.sql    denormalized views used for natural-language queries
  ingest/          parsing, fingerprinting, normalizers, canonical vocabulary
tests/             pytest suite (each test uses its own temporary database)
input_data/        sample inventory exports
docs/ROADMAP.md    build plan and design decisions
```

## Data model

Every configuration item (CI) has one row in `configuration_items` holding the shared fields, plus one row in its type table (`devices`, `users` or `apps`) with the same id (joined-table inheritance). `relationships` links any two CIs, so one table covers device ownership, app usage and app integrations.

```mermaid
erDiagram
    configuration_items {
        int id PK
        string ci_type "device | user | app"
        string name "hostname / full name / app name"
        string status "active | inactive | suspended | unknown"
        json attributes "unmapped source fields"
        json field_provenance "per-field observed_at + source_record_id"
        datetime created_at
        datetime updated_at
    }

    devices {
        int ci_id PK, FK
        string hostname
        string ip_address
        string mac_address
        string os_family
        string os_version
        string location
        bool encryption_enabled
        string device_type
        string serial_number
        datetime last_seen
    }

    users {
        int ci_id PK, FK
        string email
        string department
        string team
        json groups
        bool mfa_enabled
        datetime last_login
    }

    apps {
        int ci_id PK, FK
        string vendor
        string app_type "saas | on_prem"
        string category
        string owner_team
        int reported_usage_count
        bool sso_enabled
        bool is_stub "only seen as a reference"
    }

    relationships {
        int id PK
        int source_ci_id FK
        int target_ci_id FK
        string rel_type "OWNS | USES | INTEGRATES_WITH | DEPENDS_ON"
        float confidence "0..1, from entity resolution"
        int ingestion_id FK
        datetime created_at
    }

    ci_identifiers {
        int id PK
        int ci_id FK
        string kind "email | employee_id | external_id | serial | mac | hostname | asset_tag | alias"
        string source_system "issuing system for external_id, empty for org-wide keys"
        string value "UNIQUE(kind, source_system, value)"
    }

    source_records {
        int id PK
        int ingestion_id FK
        int ci_id FK "primary CI; set to NULL if the CI is deleted"
        int record_index
        json raw
        json normalized
        datetime observed_at
    }

    ingestion_runs {
        int id PK
        string filename
        string detected_format
        string fingerprint
        string status
        json stats
        string error
        datetime created_at
        datetime finished_at
    }

    schema_mappings {
        string fingerprint PK
        string ci_type
        string record_path
        json spec
        string created_by "llm | manual"
        datetime created_at
        datetime updated_at
    }

    configuration_items ||--o| devices : "is a"
    configuration_items ||--o| users : "is a"
    configuration_items ||--o| apps : "is a"

    configuration_items ||--o{ relationships : "source of"
    configuration_items ||--o{ relationships : "target of"

    configuration_items ||--o{ ci_identifiers : "known as"
    configuration_items ||--o{ source_records : "observed in"

    ingestion_runs ||--o{ source_records : "produced"
    ingestion_runs ||--o{ relationships : "created"
    schema_mappings ||--o{ ingestion_runs : "fingerprint used by"
```

Key design choices:

- **Identity comes from strong keys, never names.** Two users can both be called "John Doe". Emails, serials, employee IDs and source-system IDs live in `ci_identifiers`, and each can belong to only one CI.
- **System-issued IDs are scoped to their system.** Okta's `u_110` and another tool's `u_110` are unrelated, so `external_id` identifiers carry a `source_system`. Org-wide keys (email, serial, MAC) leave it empty so they match across sources.
- **Conflicts are resolved per field, and the latest observation wins.** `field_provenance` records which source record set each value and when that record was observed.
- **Source records are permanent history.** Deleting a CI detaches its raw records instead of deleting them, so they remain available for audit and for rebuilding CIs.
- **A device has at most one owner,** enforced by a partial unique index on `OWNS` relationships.
- **`schema_mappings` → `ingestion_runs` is a logical link,** not a foreign key: a run records the fingerprint it matched, and that fingerprint's mapping may be created partway through the run.

Note: this project is to be continued.
