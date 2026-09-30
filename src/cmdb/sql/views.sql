-- Denormalized, read-only views over the CI model. These are the tables the /ask
-- text-to-SQL step queries, so column names are chosen to be self-explanatory.
-- Recreated on every startup by cmdb.db.init_db().
-- `attributes` is the JSON object of source fields without a typed column (cost, contract
-- dates, hardware specs, ...); /ask tells the LLM which keys exist.

DROP VIEW IF EXISTS v_devices;
DROP VIEW IF EXISTS v_users;
DROP VIEW IF EXISTS v_apps;
DROP VIEW IF EXISTS v_user_apps;
DROP VIEW IF EXISTS v_app_integrations;
DROP VIEW IF EXISTS v_relationships;

-- One row per device, with its owner (at most one, enforced by uq_single_owner).
CREATE VIEW v_devices AS
SELECT
    ci.id                  AS device_id,
    d.hostname             AS hostname,
    ci.status              AS status,
    d.ip_address           AS ip_address,
    d.mac_address          AS mac_address,
    d.os_family            AS os_family,
    d.os_version           AS os_version,
    d.location             AS location,
    d.encryption_enabled   AS encryption_enabled,
    d.device_type          AS device_type,
    d.serial_number        AS serial_number,
    d.last_seen            AS last_seen,
    owner.id               AS owner_id,
    owner.name             AS owner_name,
    owner_user.email       AS owner_email,
    ci.attributes          AS attributes
FROM configuration_items AS ci
JOIN devices AS d ON d.ci_id = ci.id
LEFT JOIN relationships AS r ON r.target_ci_id = ci.id AND r.rel_type = 'OWNS'
LEFT JOIN configuration_items AS owner ON owner.id = r.source_ci_id
LEFT JOIN users AS owner_user ON owner_user.ci_id = owner.id;

-- One row per user, with counts of apps used and devices owned.
CREATE VIEW v_users AS
SELECT
    ci.id           AS user_id,
    ci.name         AS name,
    u.email         AS email,
    ci.status       AS status,
    u.department    AS department,
    u.team          AS team,
    u.groups        AS groups,
    u.mfa_enabled   AS mfa_enabled,
    u.last_login    AS last_login,
    (SELECT COUNT(*) FROM relationships r
      WHERE r.source_ci_id = ci.id AND r.rel_type = 'USES') AS app_count,
    (SELECT COUNT(*) FROM relationships r
      WHERE r.source_ci_id = ci.id AND r.rel_type = 'OWNS') AS device_count,
    ci.attributes   AS attributes
FROM configuration_items AS ci
JOIN users AS u ON u.ci_id = ci.id;

-- One row per app. reported_usage_count comes from the source system;
-- observed_user_count is the number of users we have actually linked to the app.
CREATE VIEW v_apps AS
SELECT
    ci.id                    AS app_id,
    ci.name                  AS name,
    ci.status                AS status,
    a.vendor                 AS vendor,
    a.app_type               AS app_type,
    a.category               AS category,
    a.owner_team             AS owner_team,
    a.reported_usage_count   AS reported_usage_count,
    (SELECT COUNT(*) FROM relationships r
      WHERE r.target_ci_id = ci.id AND r.rel_type = 'USES') AS observed_user_count,
    a.sso_enabled            AS sso_enabled,
    a.is_stub                AS is_stub,
    ci.attributes            AS attributes
FROM configuration_items AS ci
JOIN apps AS a ON a.ci_id = ci.id;

-- One row per (user, app) usage.
CREATE VIEW v_user_apps AS
SELECT
    u_ci.id        AS user_id,
    u_ci.name      AS user_name,
    u.email        AS user_email,
    a_ci.id        AS app_id,
    a_ci.name      AS app_name,
    r.confidence   AS confidence
FROM relationships AS r
JOIN configuration_items AS u_ci ON u_ci.id = r.source_ci_id
JOIN users AS u ON u.ci_id = u_ci.id
JOIN configuration_items AS a_ci ON a_ci.id = r.target_ci_id
WHERE r.rel_type = 'USES';

-- Integrations are symmetric, but sources usually list them on one side only
-- (Jenkins lists GitHub; GitHub does not list Jenkins), so expose both directions.
CREATE VIEW v_app_integrations AS
SELECT a.id AS app_id, a.name AS app_name, b.id AS integrated_app_id, b.name AS integrated_app_name
FROM relationships AS r
JOIN configuration_items AS a ON a.id = r.source_ci_id
JOIN configuration_items AS b ON b.id = r.target_ci_id
WHERE r.rel_type = 'INTEGRATES_WITH'
UNION
SELECT b.id, b.name, a.id, a.name
FROM relationships AS r
JOIN configuration_items AS a ON a.id = r.source_ci_id
JOIN configuration_items AS b ON b.id = r.target_ci_id
WHERE r.rel_type = 'INTEGRATES_WITH';

-- Every relationship with both ends resolved to type and name.
CREATE VIEW v_relationships AS
SELECT
    r.id          AS relationship_id,
    r.rel_type    AS rel_type,
    s.id          AS source_id,
    s.ci_type     AS source_type,
    s.name        AS source_name,
    t.id          AS target_id,
    t.ci_type     AS target_type,
    t.name        AS target_name,
    r.confidence  AS confidence
FROM relationships AS r
JOIN configuration_items AS s ON s.id = r.source_ci_id
JOIN configuration_items AS t ON t.id = r.target_ci_id;
