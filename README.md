# stlabs-assessment-backend-cmbd
STLabs Take Home (Backend CMDB)

## Data Model

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

Notes:
- **Names are not unique.** Two users can both be called "John Doe". Identity comes only from strong keys in `ci_identifiers` (emails, employee IDs, serials, source-system IDs), each of which can belong to only one CI (`UNIQUE(kind, source_system, value)`).
- **System-issued IDs are scoped to their system.** Okta's user `u_110` and another tool's `u_110` are unrelated, so `external_id` identifiers carry a `source_system`. Org-wide keys (email, serial, MAC, ...) leave it empty so they match across sources.
- **Source records are permanent history.** Deleting a CI detaches its raw records (`ci_id` becomes NULL) instead of deleting them, so they stay available for audit and for rebuilding CIs.
- **A device has at most one owner.** A partial unique index on `relationships(target_ci_id) WHERE rel_type = 'OWNS'` enforces this.
- The link from `schema_mappings` to `ingestion_runs` is logical rather than a foreign key: a run records the fingerprint it matched, and that fingerprint's mapping may only be created partway through the run.
