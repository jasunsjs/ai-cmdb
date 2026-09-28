# Roadmap: AI-First CMDB

The build plan for the take-home described in [`STLabs_Backend_TakeHome_CMDB_Junior.md`](STLabs_Backend_TakeHome_CMDB_Junior.md), and a record of the design decisions made along the way. "Polish" items (UI, docker-compose) are out of scope until the core is done.

## Status

| Phase | Scope | New dependencies | Status |
|---|---|---|---|
| 0 | Scaffolding: uv project, FastAPI app with `/health`, pytest, ruff | `fastapi`, `uvicorn[standard]`, `pydantic`, `pydantic-settings`; dev: `pytest`, `httpx2`, `ruff` | Done |
| 1 | Data model and DB layer: ORM models, SQLite setup, views | `sqlalchemy` | Done |
| 2 | Parsing and normalization (no AI, no DB) | `pyyaml` | In progress |
| 3 | LLM client for OpenRouter, plus a fake for tests | `openai` | Planned |
| 4 | Ingestion pipeline and `POST /ingest` | `python-multipart`, `rapidfuzz` | Planned |
| 5 | `POST /ask` (text-to-SQL) | none (stdlib `sqlite3`) | Planned |
| 6 | Read endpoints, demo script, README, session transcripts | none | Planned |

Each phase adds only the dependencies it uses. Anything the code imports is declared directly in `pyproject.toml`, never relied on transitively.

## What the sample data demands

| Problem | Evidence in `input_data/` |
|---|---|
| Same entities, different formats | The same 3 devices appear in CSV (flat), JSON (flat, sparse) and YAML (nested under `inventory.devices`) |
| Conflicting values | John's device location: London (CSV 7/22, YAML 7/23) vs New York HQ (JSON 6/12). Status: `active` / `inactive` / `deactivated` |
| Unnormalized values | OS `macos` / `macOS Ventura` / `Ubuntu 22.04 LTS`. Encryption `FileVault Enabled` vs `true`. Status `ACTIVE` / `SUSPENDED` |
| Identity resolution | Okta `john.d@example.com` vs YAML `john.doe@example.com` vs CSV `John D.` |
| App name aliasing | Okta `GitHub` / `Salesforce` vs inventory `GitHub Enterprise` / `Salesforce CRM` |
| Dangling references | Okta apps and integration targets that aren't in the app inventory |
| Implicit relationships | `assigned_to` → device ownership, Okta `apps` → app usage, `integrations` → app dependencies |

## Tech stack

| Concern | Choice |
|---|---|
| Package and environment | uv, with committed `uv.lock` and `.python-version` (3.13) |
| Web | FastAPI + uvicorn |
| Database | SQLite (`data/cmdb.db`, created on startup), with WAL mode and foreign keys on |
| ORM | SQLAlchemy 2.x, used synchronously. `create_all` at startup, no migrations yet |
| Config | pydantic-settings. Precedence: environment variables, then `.env`, then defaults in `config.py` |
| LLM | `openai` SDK pointed at OpenRouter. The model comes from `OPENROUTER_MODEL` |
| Fuzzy matching | rapidfuzz |
| Tests and lint | pytest (tests never need an API key), ruff |

New-machine setup needs only uv: `cp .env.example .env`, add the API key, then `uv run cmdb-serve`.

## Data model decisions

See the ER diagram in the [README](../README.md#data-model).

- **Joined-table inheritance.** `configuration_items` holds the shared fields, and `devices` / `users` / `apps` share its primary key. Relationships, identifiers and source records all have a real foreign key to one table, while type-specific fields stay typed and indexable.
- **Relationships are rows** in one typed edge table (`OWNS`, `USES`, `INTEGRATES_WITH`, `DEPENDS_ON`), each with a `confidence`. A partial unique index allows at most one `OWNS` per device.
- **Names are never keys.** Two users can share a name. Identity comes only from `ci_identifiers`, which has `UNIQUE(kind, source_system, value)`.
- **`source_system` scopes system-issued IDs.** It's required for `external_id` and empty for org-wide keys (email, serial, MAC, …), so those still match across sources. It's an empty string rather than NULL because NULLs never collide in a UNIQUE constraint.
- **Source records are permanent history.** Deleting a CI sets their `ci_id` to NULL instead of deleting them, so they remain for audit and for rebuilding CIs.
- **Field-level merge: the latest observation wins.** `field_provenance` records the `observed_at` and source record behind each stored value.
- **`updated_at` is bumped on any column change,** including subclass-only columns, through a `before_update` event.
- **Usage is kept twice:** `reported_usage_count` from the source, and `observed_user_count` derived from `USES` edges.
- **Stub apps** (`is_stub`) keep references to apps that are known only by name.
- **Views** (`v_devices`, `v_users`, `v_apps`, `v_user_apps`, `v_app_integrations`, `v_relationships`) flatten the model for `/ask`.

## Ingestion pipeline

```
upload → parse → fingerprint → get mapping → apply mapping → normalize → resolve → write → report
                                   │                            │            │
                            schema_mappings              CANONICAL_FIELDS  ci_identifiers
                            (+ LLM on a miss)            (+ LLM for         (+ LLM for the
                                                          unknown values)    ambiguous band)
```

1. **Parse** (Phase 2). The format is detected, the record list located (`applications`, `inventory.devices`, a root array) and file metadata collected. YAML is loaded as text only (`BaseLoader`), so values like `version: 14.10` and `country: NO` aren't silently changed.
2. **Fingerprint** (Phase 2). A hash of the record path plus the sorted union of key paths. Values and file format don't affect it.
3. **Mapping** (Phase 4). Looked up in `schema_mappings` by fingerprint. On a miss, the LLM sees the key paths, a few sample records and the canonical vocabulary, and returns a mapping that is validated and saved. The LLM is called once per new format, never per record.
4. **Apply mapping** (Phase 4). Plain Python renames source fields to canonical fields. Values aren't changed at this step.
5. **Normalize** (Phase 2). Each canonical field's normalizer cleans its value. Values the rules can't classify are collected and resolved by the LLM in one batched call (Phase 4).
6. **Resolve** (Phase 4, option A). Strong identifiers match first. Hard vetoes (such as conflicting employee IDs) never merge. The LLM judges only the ambiguous band and returns `{match, confidence, reason}`. It auto-merges when `confidence ≥ ENTITY_MATCH_AUTO_MERGE_THRESHOLD` (default 0.85; a value above 1 makes it suggest-only). When unsure, or when there's no API key, records are kept separate and flagged. Accepted matches store the new identifier, so the next upload matches through the rules.
7. **Write and report** (Phase 4). CIs, identifiers, source records, provenance and relationships are written in one transaction. The response lists what was created and merged, along with every AI decision and its reason.

**Canonical vocabulary** (`ingest/canonical.py`). The fields, identifiers and relations each CI type supports, defined once in code. Mappings in `schema_mappings` translate each source format *into* this vocabulary.

## `/ask` design (Phase 5)

The LLM gets the view schema plus a few examples and returns SQL (or a clarification). The SQL is checked (single statement, reads-only authorizer, read-only connection, enforced `LIMIT`, timeout) and then run, with one repair retry if it errors. A second LLM call summarizes the rows. The response always includes the SQL.

## Open items

- **Where `source_system` comes from for `external_id` identifiers** (a form field, file metadata or the mapping). Decided in Phase 4.

## Known limitations and future work

- **Fingerprints are exact.** A new field, or a sparse export that happens to omit an optional field, gives a new fingerprint and costs one extra LLM call. Reusing the closest existing mapping would avoid this.
- **One record list per file.** Only the largest list of records in a document is ingested.
- **Naive timestamps are assumed to be UTC.**
- **YAML values stay text** in `attributes` (for example `ram_gb: "32"`).
- **Relative SQLite path.** `data/cmdb.db` resolves against the directory the server is started from.
- **No migrations yet.** After a schema change, delete `data/cmdb.db`. Alembic is the production path.
- **Future work:** a duplicate-review queue with merge and split endpoints; raw payloads moved to object storage with retention; async ingestion (return `202` and poll the run).
