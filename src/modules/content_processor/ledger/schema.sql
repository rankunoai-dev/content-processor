-- Content processor ledger (ARCHITECTURE.md §5, §7, §9, §11). Postgres 14+.
-- Idempotent: safe to run on every boot.

CREATE TABLE IF NOT EXISTS runs (
    run_id            TEXT PRIMARY KEY,
    tenant_id         TEXT NOT NULL,
    scope_hash        TEXT NOT NULL,
    scope             JSONB NOT NULL,
    profile           TEXT NOT NULL,
    tool_versions     JSONB NOT NULL DEFAULT '{}'::jsonb,
    state             TEXT NOT NULL DEFAULT 'ADMITTED',
    cancel_requested  BOOLEAN NOT NULL DEFAULT FALSE,
    stop_reason       TEXT,
    cost_ceiling_usd  NUMERIC(12,4) NOT NULL DEFAULT 0,
    cost_spent_usd    NUMERIC(12,4) NOT NULL DEFAULT 0,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at       TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS host_policy (
    tenant_id             TEXT NOT NULL,
    host                  TEXT NOT NULL,
    max_concurrency       INT NOT NULL DEFAULT 1,
    in_flight             INT NOT NULL DEFAULT 0,
    base_delay_ms         INT NOT NULL DEFAULT 1000,
    next_allowed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    circuit_state         TEXT NOT NULL DEFAULT 'CLOSED',
    circuit_opened_at     TIMESTAMPTZ,
    consecutive_failures  INT NOT NULL DEFAULT 0,
    clean_streak          INT NOT NULL DEFAULT 0,
    block_score           REAL NOT NULL DEFAULT 0,
    access_tier           TEXT NOT NULL DEFAULT 'T1_POLITE_HTTP',
    tier_ceiling          TEXT NOT NULL,
    session_generation    INT NOT NULL DEFAULT 0,
    robots_cache          TEXT,
    robots_fetched_at     TIMESTAMPTZ,
    crawl_delay_ms        INT,
    retry_after_until     TIMESTAMPTZ,
    legal_hold            BOOLEAN NOT NULL DEFAULT FALSE,
    unlocker_requests     INT NOT NULL DEFAULT 0,
    proxy_bytes           BIGINT NOT NULL DEFAULT 0,
    baseline_main_words   INT,
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, host)
);

CREATE TABLE IF NOT EXISTS tasks (
    id                BIGSERIAL PRIMARY KEY,
    run_id            TEXT NOT NULL REFERENCES runs(run_id),
    tenant_id         TEXT NOT NULL,
    kind              TEXT NOT NULL,
    idempotency_key   TEXT NOT NULL UNIQUE,
    host              TEXT NOT NULL,
    priority          SMALLINT NOT NULL DEFAULT 1,
    state             TEXT NOT NULL DEFAULT 'PENDING',
    attempts          INT NOT NULL DEFAULT 0,
    max_attempts      INT NOT NULL DEFAULT 3,
    not_before        TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_owner       TEXT,
    lease_expires_at  TIMESTAMPTZ,
    heartbeat_at      TIMESTAMPTZ,
    holds_host_slot   BOOLEAN NOT NULL DEFAULT FALSE,
    payload           JSONB NOT NULL DEFAULT '{}'::jsonb,
    checkpoint        JSONB,
    last_error        TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at       TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS tasks_pending_idx ON tasks (kind, priority, id) WHERE state = 'PENDING';
CREATE INDEX IF NOT EXISTS tasks_leased_idx  ON tasks (lease_expires_at) WHERE state = 'LEASED';
CREATE INDEX IF NOT EXISTS tasks_run_host_idx ON tasks (run_id, host);

CREATE TABLE IF NOT EXISTS resources (
    resource_id     TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL REFERENCES runs(run_id),
    schedule_key    TEXT NOT NULL,
    requested_url   TEXT NOT NULL,
    final_url       TEXT,
    host            TEXT NOT NULL,
    mandatory       BOOLEAN NOT NULL DEFAULT FALSE,
    status          TEXT NOT NULL DEFAULT 'DISCOVERED',
    status_reason   TEXT,
    payload_sha256  TEXT,
    acquired_at_tier TEXT,
    expected_mime   TEXT,
    expected_mime_source TEXT,
    record          JSONB NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, schedule_key)
);
CREATE INDEX IF NOT EXISTS idx_resources_payload ON resources (run_id, payload_sha256);
CREATE INDEX IF NOT EXISTS idx_resources_run ON resources (run_id);

CREATE TABLE IF NOT EXISTS resource_edges (
    run_id      TEXT NOT NULL REFERENCES runs(run_id),
    parent_id   TEXT NOT NULL,
    child_id    TEXT NOT NULL,
    edge_kind   TEXT NOT NULL,   -- embeds | links_to | frames | attachment_of | redirects_to
    evidence_id TEXT,
    PRIMARY KEY (run_id, parent_id, child_id, edge_kind)
);
CREATE INDEX IF NOT EXISTS idx_edges_parent ON resource_edges (run_id, parent_id);
CREATE INDEX IF NOT EXISTS idx_edges_child ON resource_edges (run_id, child_id);

CREATE TABLE IF NOT EXISTS states (
    state_id        TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL REFERENCES runs(run_id),
    resource_id     TEXT NOT NULL,
    parent_state_id TEXT,
    state_key       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'DISCOVERED',
    record          JSONB NOT NULL,
    UNIQUE (run_id, resource_id, state_key)
);
CREATE INDEX IF NOT EXISTS idx_states_resource ON states (run_id, resource_id);

CREATE TABLE IF NOT EXISTS regions (
    region_id    TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL REFERENCES runs(run_id),
    resource_id  TEXT NOT NULL,
    state_id     TEXT,
    region_key   TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'DISCOVERED',
    record       JSONB NOT NULL,
    UNIQUE (run_id, resource_id, state_id, region_key)
);
CREATE INDEX IF NOT EXISTS idx_regions_resource ON regions (run_id, resource_id);


CREATE TABLE IF NOT EXISTS extractions (
    extraction_id TEXT PRIMARY KEY,
    run_id        TEXT NOT NULL REFERENCES runs(run_id),
    region_id     TEXT NOT NULL,
    block_key     TEXT NOT NULL,
    method        TEXT NOT NULL,
    tool_version  TEXT NOT NULL,
    record        JSONB NOT NULL,
    UNIQUE (run_id, region_id, method)
);
CREATE INDEX IF NOT EXISTS extractions_block_key_idx ON extractions (block_key);

CREATE TABLE IF NOT EXISTS evidence (
    evidence_id  TEXT PRIMARY KEY,
    tenant_id    TEXT NOT NULL,
    sha256       TEXT NOT NULL,
    storage_uri  TEXT NOT NULL,
    media_type   TEXT NOT NULL,
    bytes        BIGINT NOT NULL,
    captured_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    redacted     BOOLEAN NOT NULL DEFAULT FALSE,
    retention_class TEXT NOT NULL DEFAULT 'standard_90d'
);
CREATE INDEX IF NOT EXISTS evidence_sha_idx ON evidence (tenant_id, sha256);

CREATE TABLE IF NOT EXISTS gaps (
    gap_id       TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL REFERENCES runs(run_id),
    resource_id  TEXT NOT NULL,
    gap_type     TEXT NOT NULL,
    status       TEXT NOT NULL,
    record       JSONB NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS gaps_run_idx ON gaps (run_id, resource_id);

CREATE TABLE IF NOT EXISTS events (
    seq                BIGSERIAL PRIMARY KEY,
    run_id             TEXT NOT NULL,
    entity_type        TEXT NOT NULL,
    entity_id          TEXT NOT NULL,
    old_state          TEXT,
    new_state          TEXT NOT NULL,
    inventory_revision INT NOT NULL DEFAULT 0,
    reason             TEXT,
    attempt            INT NOT NULL DEFAULT 0,
    evidence_ids       JSONB NOT NULL DEFAULT '[]'::jsonb,
    at                 TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS events_run_seq_idx ON events (run_id, seq);

CREATE TABLE IF NOT EXISTS cost_ledger (
    id          BIGSERIAL PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES runs(run_id),
    tenant_id   TEXT NOT NULL,
    vendor      TEXT NOT NULL,
    route       TEXT NOT NULL,
    amount_usd  NUMERIC(12,6) NOT NULL,
    units       TEXT NOT NULL,
    quantity    NUMERIC(14,4) NOT NULL,
    at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS cost_ledger_day_idx ON cost_ledger (tenant_id, at);
