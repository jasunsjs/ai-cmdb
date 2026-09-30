# AI CMDB

An AI-assisted Configuration Management Database (CMDB) backend. Upload raw IT and SaaS inventory exports (hardware lists, Okta user dumps, application inventories) as JSON, YAML or CSV. The service turns them into one clean model of **devices, users and apps** and the **relationships** between them: who owns which laptop, who uses which app, and which apps integrate with each other. You can then ask questions about it in plain English, such as *"Which users don't have MFA?"*

Inventory data is messy. The same laptop appears in several exports with different shapes, one file names its owner `John D.` and another `john.doe@example.com`, and OS strings range from `macos` to `macOS Sonoma 14.5`. Deterministic rules do most of the cleanup. An LLM is used only where rules fall short:
- mapping a file format it hasn't seen before (once per format, never per record);
- interpreting values the rules can't classify;
- judging ambiguous identity matches;
- turning questions into read-only SQL.

Every AI decision is recorded with its reasoning, and every answer comes with the SQL that produced it.

## Setup

**1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/).** It's the only prerequisite; uv installs the right Python version and all dependencies itself.

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

**2. Clone the repository and add your API key.** LLM access goes through [OpenRouter](https://openrouter.ai/keys).

```bash
git clone <repository-url> ai-cmdb
cd ai-cmdb
cp .env.example .env        # Windows: copy .env.example .env
# edit .env and set OPENROUTER_API_KEY
```

**3. Start the server.** The first run creates the virtual environment and the SQLite database (`data/cmdb.db`).

```bash
uv run cmdb-serve           # http://127.0.0.1:8000, interactive API docs at /docs
```

**4. Try it.** In a second terminal, run the demo. It uploads the sample files in `input_data/`, asks a set of questions, and saves every response to `examples/`.

```bash
uv run cmdb-demo
```

Run all commands from the repository root. To start over with an empty database, stop the server and delete `data/cmdb.db`.

Without an API key, the sample files still ingest using built-in mappings, but new file formats and `/ask` need the LLM.

## API

| Endpoint | Purpose |
|---|---|
| `POST /ingest` | Upload a JSON, YAML or CSV file (multipart field `file`). Returns a report of what was created, merged and decided |
| `POST /ask` | `{"question": "..."}` returns an answer, the SQL used, and the rows |
| `GET /cis`, `GET /cis/{id}` | Browse configuration items, with their identifiers, relationships and field-level provenance |
| `GET /ingestions`, `GET /mappings` | Past uploads and the cached field mappings for each file format |

## Configuration

Settings are read from environment variables or `.env`. Only `OPENROUTER_API_KEY` is required. All options, with their defaults, are listed in `.env.example`.

| Variable | Default | Purpose |
|---|---|---|
| `OPENROUTER_API_KEY` | none | Enables the LLM features |
| `OPENROUTER_MODEL` | `anthropic/claude-haiku-4.5` | Model used for all LLM calls |
| `DATABASE_URL` | `sqlite:///data/cmdb.db` | Where data is stored |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Server address |

## Tests

```bash
uv run pytest               # full suite; no API key or network needed
uv run pytest -m live       # optional: end-to-end checks against the real LLM (needs the key)
```

## Tech stack

Python 3.13, FastAPI, SQLAlchemy 2 on SQLite, Pydantic, pytest and ruff, all managed with uv. LLM access goes through OpenRouter using the OpenAI SDK.
