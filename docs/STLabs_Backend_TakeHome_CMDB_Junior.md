# Backend Take-Home Assignment

## Build an AI-First Configuration Management Database (CMDB)

**Recommended timebox:** 6 hours
**Focus:** Data modeling, AI integration, backend systems thinking
**Language:** Python
**AI usage:** Strongly encouraged with session transcripts

---

## What we are looking for

We care most about your ability to model complex, interconnected data and apply AI thoughtfully to real problems. A well-reasoned data model with clear tradeoffs is better than a broad feature set with shallow coverage. We evaluate the `/ingest` and `/ask` endpoints most heavily.

We look holistically at: code quality and structure, AI integration and reasoning, data modeling and storage choices, API design, and documentation. There are no fixed weights — strong work in any area can stand out.

While we do not expect a production-ready system in the submission, your design would need to be the foundation of one. Thinking about how to scale, what to optimize, metrics to observe, and design tradeoffs for such a system would be the key discussion points on your submission.

## Context

You are building a runnable prototype of an AI-powered Configuration Management Database (CMDB) backend service. The system ingests raw or semi-structured IT infrastructure data, extracts and normalizes configuration items (CIs), stores them in a database, and uses AI to assist when necessary.

# Core Scope (required)

## Objective

Your solution should:

1. Ingest raw or semi-structured data from IT infrastructure and/or third-party SaaS applications.
2. Extract and normalize configuration items (CIs).
3. Store the structured data in any database of your choice.
4. Use AI to assist with one or more of the following:
   - Parsing and structuring input data.
   - Enriching missing or inconsistent fields.
   - Responding to natural language queries.

## Input Data Sources

We've provided some sample input data, but feel free to find/generate more of your own if you wish. The data could be any of the following:

### Physical Infrastructure Examples

- JSON exports from Jamf, SCCM, or GLPI.
- CSV or YAML files representing hardware inventory.
- SNMP/NetBox-style records (e.g., hostname, MAC address, physical location).
- Simulated or manually generated hardware data.

### SaaS Application Examples

- Okta user and app assignment exports.
- GitHub organization metadata.
- Atlassian/Jira project exports.
- Salesforce or Workday usage reports.
- Slack workspace configuration data.

## Configuration Items (CIs) to Extract

From the input data, extract and normalize the following:

### Device

- Hostname, IP address, OS, assigned user, location, status.

### User

- Name, team, assigned applications, MFA status, last login.

### App

- Name, owner, type (SaaS/on-prem), integrations, usage count.

### Relationships

- Device ownership.
- App usage.
- Dependencies between different Apps/Devices

## Storage

Use any database of your choice. Clearly justify your data model and technology choice in the README. Some options include:

- SQL: PostgreSQL, MySQL, SQLite.
- Graph DB: Neo4j, etc.
- NoSQL: MongoDB, DynamoDB.
- Embedded: TinyDB, Redis.

## API Requirements

Build a REST API using Python that includes:

- `POST /ingest`: Upload raw input data.
- `POST /ask`: Handle natural language queries (e.g., "Which users don't have MFA?").

## AI Usage

You may use OpenAI, HuggingFace, LangChain, or similar tools. You may also mock AI responses where needed — just be sure to explain your approach.

---

# Implementation Guidelines

## What to prioritize

- A clear, well-justified data model for CIs and their relationships
- Thoughtful AI integration that adds real value
- Clean API design with good separation of concerns
- Reasoning about tradeoffs in your README

## What we do not expect

- Perfect coverage of every edge case — pick a coherent slice and document choices

---

# OpenRouter

We will be providing you with an OpenRouter API key you can use to run/test your service. It gives you access to hundreds of AI models through a single endpoint. You can find the quickstart guide here: [https://openrouter.ai/docs/quickstart](https://openrouter.ai/docs/quickstart).

Additionally, we encourage you to use the same token to run a coding agent to help you with this assignment. For example, here is the guide to setup Claude Code: [https://openrouter.ai/docs/guides/guides/claude-code-integration](https://openrouter.ai/docs/guides/guides/claude-code-integration). Feel free to use any coding agent to help you if you want.

---

# Sample Outputs

## Normalized Device CI

```json
{
  "ci_type": "device",
  "hostname": "laptop-jdoe",
  "ip_address": "10.10.22.5",
  "os": "macOS",
  "assigned_user": "John Doe",
  "location": "New York HQ",
  "encryption": true,
  "status": "active"
}
```

## User Relationship from Okta

```json
{
  "ci_type": "user",
  "name": "Jane Doe",
  "email": "jane.d@example.com",
  "apps": ["GitHub", "Slack", "Salesforce"],
  "mfa_enabled": true,
  "status": "active",
  "last_login": "2024-07-15T10:03:00Z"
}
```

# Deliverables

Send a zip file containing: 
  - Source code
  - Sample API responses, test cases, or a demo script.
  - A sample session (or all sessions) from your AI assisted coding sessions. Include session transcripts from Claude Code, Codex, OpenCode, Cursor or whatever your tool of choice is.
  - `README.md` including:
    - Setup instructions.
    - Architecture and data model overview.
    - Database technology choice and justification.
    - AI usage description.
    - Assumptions or limitations.

**If you use Github to track your work please keep the repo private**

## How we will evaluate the work

1. **Setup & Runnability.** How easy is it to run your project ourselves and test the behavior?
2. **Database Design.** How you model the different entities and their relationships so that the data is queryable and maintainable.
3. **Code Quality.** Does your code follow best practices in terms of structure, error handling, etc?
4. **Polish (bonus).** Anything beyond the spec that shows ownership: a simple UI, docker-compose that wires everything together, features we didn't ask for but you want to show off.


## The review

We will spend 45 minutes together using and discussing your submission. Be
prepared to explain your choices and walk through your ingestion and ask logic.

The review is collaborative, not a presentation exercise.