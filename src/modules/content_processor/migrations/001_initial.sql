-- ============================================================================
-- Content Processor: Initial Migration (001_initial.sql)
-- Postgres 15+ compatible DDL
-- Single Source of Truth for Runs, Tasks, Policies, Extractions, and Gaps.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- 1. Runs Table
CREATE TABLE IF NOT EXISTS runs (
    run_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           TEXT NOT NULL,
    scope_json          JSONB NOT NULL,
    scope_hash          TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'running',
    cancel_requested    BOOLEAN NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at          TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    total_urls          INT NOT NULL DEFAULT 0,
    terminal_urls       INT NOT NULL DEFAULT 0,
    cost_usd            NUMERIC(10, 4) NOT NULL DEFAULT 0.0000,
    tool_versions       JSONB NOT NULL DEFAULT '{}'::jsonb,
    stop_reason         TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_tenant_status ON runs(tenant_id, status);

-- 2. Tasks Table (Durable Queue via FOR UPDATE SKIP LOCKED)
CREATE TABLE IF NOT EXISTS tasks (
    task_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id              UUID NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    task_type           TEXT NOT NULL,
    priority            INT NOT NULL DEFAULT 50 CHECK (priority >= 0 AND priority <= 100),
    status              TEXT NOT NULL DEFAULT 'queued',
    idempotency_key     TEXT UNIQUE NOT NULL,
    payload             JSONB NOT NULL,
    checkpoint          JSONB,
    lease_worker_id     TEXT,
    lease_expires_at    TIMESTAMPTZ,
    attempts            INT NOT NULL DEFAULT 0,
    max_attempts        INT NOT NULL DEFAULT 3,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Primary task lease query index:
-- SELECT ... FROM tasks WHERE status = 'queued' AND run_id = $1 ORDER BY priority DESC, created_at ASC LIMIT 1 FOR UPDATE SKIP LOCKED
CREATE INDEX IF NOT EXISTS idx_tasks_lease ON tasks (run_id, status, priority DESC, created_at ASC);
CREATE INDEX IF NOT EXISTS idx_tasks_reaper ON tasks (status, lease_expires_at) WHERE status = 'in_progress';

-- 3. Host Policies Table (Per-host rate limit & circuit breaker)
CREATE TABLE IF NOT EXISTS host_policies (
    run_id              UUID NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    host                TEXT NOT NULL,
    concurrency_limit   INT NOT NULL DEFAULT 2,
    delay_ms            INT NOT NULL DEFAULT 1000,
    crawl_delay_ms      INT NOT NULL DEFAULT 0,
    circuit_state       TEXT NOT NULL DEFAULT 'closed',
    circuit_open_until  TIMESTAMPTZ,
    access_tier         TEXT NOT NULL DEFAULT 't1_http',
    block_score         INT NOT NULL DEFAULT 0,
    total_requests      INT NOT NULL DEFAULT 0,
    challenge_count     INT NOT NULL DEFAULT 0,
    legal_hold          BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, host)
);

-- 4. URL Records Table
CREATE TABLE IF NOT EXISTS url_records (
    url_record_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id              UUID NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    schedule_key        TEXT NOT NULL,
    requested_url       TEXT NOT NULL,
    final_url           TEXT,
    capture_seq         INT NOT NULL DEFAULT 1,
    badge               TEXT NOT NULL,
    status_vector       JSONB NOT NULL,
    discovery           JSONB NOT NULL,
    inventory           JSONB NOT NULL,
    gaps_list_is_abbreviated BOOLEAN NOT NULL DEFAULT FALSE,
    extraction_methods_used TEXT[] NOT NULL DEFAULT '{}',
    access_tier_used    TEXT NOT NULL,
    scope_hash          TEXT NOT NULL,
    captured_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    schema_version_tools JSONB NOT NULL DEFAULT '{}'::jsonb,
    warnings            TEXT[] NOT NULL DEFAULT '{}',
    UNIQUE (run_id, schedule_key)
);

CREATE INDEX IF NOT EXISTS idx_url_records_run_badge ON url_records(run_id, badge);

-- 5. Gap Records Table
CREATE TABLE IF NOT EXISTS gap_records (
    gap_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    url_record_id       UUID NOT NULL REFERENCES url_records(url_record_id) ON DELETE CASCADE,
    region_id           TEXT NOT NULL,
    gap_type            TEXT NOT NULL,
    status              TEXT NOT NULL,
    reason              TEXT NOT NULL,
    evidence_ids        TEXT[] NOT NULL DEFAULT '{}',
    attempt_number      INT NOT NULL DEFAULT 1,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_gap_records_url ON gap_records(url_record_id);

-- 6. Content Blocks Table
CREATE TABLE IF NOT EXISTS content_blocks (
    block_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    block_key           TEXT NOT NULL,
    run_id              UUID NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    url_record_id       UUID NOT NULL REFERENCES url_records(url_record_id) ON DELETE CASCADE,
    region_id           TEXT NOT NULL,
    state_id            TEXT NOT NULL,
    frame_path          TEXT NOT NULL DEFAULT 'main',
    heading_path        TEXT[] NOT NULL DEFAULT '{}',
    layer               TEXT NOT NULL,
    reading_order_index INT NOT NULL,
    raw_text            TEXT NOT NULL,
    normalized_text     TEXT NOT NULL,
    language            TEXT,
    is_boilerplate      BOOLEAN NOT NULL DEFAULT FALSE,
    pii_detected        BOOLEAN NOT NULL DEFAULT FALSE,
    method              TEXT NOT NULL,
    method_version      TEXT NOT NULL,
    confidence          NUMERIC(4, 3) NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    status              TEXT NOT NULL,
    evidence_ids        TEXT[] NOT NULL,
    provenance          JSONB NOT NULL DEFAULT '{}'::jsonb,
    bounding_box        JSONB,
    gap_ids             TEXT[] NOT NULL DEFAULT '{}',
    is_volatile         BOOLEAN NOT NULL DEFAULT FALSE,
    attempt_number      INT NOT NULL DEFAULT 1,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_blocks_url_layer ON content_blocks(url_record_id, layer);
CREATE INDEX IF NOT EXISTS idx_blocks_key ON content_blocks(block_key);

-- 7. Run Events Table (Append-only audit trail)
CREATE TABLE IF NOT EXISTS run_events (
    event_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sequence            BIGSERIAL NOT NULL,
    run_id              UUID NOT NULL REFERENCES runs(run_id),
    url_record_id       UUID,
    entity_type         TEXT NOT NULL,
    entity_id           TEXT NOT NULL,
    previous_status     TEXT,
    new_status          TEXT NOT NULL,
    reason              TEXT,
    attempt             INT NOT NULL DEFAULT 1,
    evidence_ids        TEXT[] NOT NULL DEFAULT '{}',
    occurred_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, sequence)
);

-- 8. Cost Ledger (Strict append-only financial accounting)
CREATE TABLE IF NOT EXISTS cost_ledger (
    entry_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id              UUID NOT NULL REFERENCES runs(run_id),
    line_item           TEXT NOT NULL,
    amount_usd          NUMERIC(10, 4) NOT NULL CHECK (amount_usd >= 0),
    charged_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_cost_ledger_run ON cost_ledger(run_id);
