"""SQL statements for the task ledger (ADR-0001), kept apart from execution.

Parameters use psycopg's `%(name)s` style. Statements that consume a host slot
lock the `host_policy` row inside the same transaction so two workers can never
both take the last slot (ARCHITECTURE.md §7).
"""

from __future__ import annotations

from src.modules.content_processor.enums import TaskKind

__all__ = [
    "HOST_SLOT_KINDS",
    "SQL_APPEND_EVENT",
    "SQL_CANCEL_OPEN_TASKS",
    "SQL_CANCEL_RUN",
    "SQL_CHECKPOINT",
    "SQL_COMPLETE",
    "SQL_ENQUEUE",
    "SQL_ENSURE_HOST",
    "SQL_FAIL",
    "SQL_HEARTBEAT",
    "SQL_INSERT_RUN",
    "SQL_LEASE_PLAIN",
    "SQL_LEASE_WITH_SLOT",
    "SQL_LOAD_HOST",
    "SQL_PENDING_COUNT",
    "SQL_REAP",
    "SQL_RELEASE_SLOT",
    "SQL_RESOURCE_COUNTS",
    "SQL_SAVE_HOST",
    "SQL_SET_ROBOTS",
    "SQL_GET_BLOCKS",
    "SQL_GET_EVENTS_FOR_ENTITY",
    "SQL_GET_EVENTS_FOR_RUN",
    "SQL_GET_GAPS",
    "SQL_GET_REGIONS",
    "SQL_GET_RESOURCE",
    "SQL_GET_STATES",
    "SQL_LIST_RESOURCES",
    "SQL_OUTSTANDING_RESOURCE_IDS",
    "SQL_RECORD_EDGE",
    "SQL_RECORD_SUMMARY",
    "SQL_FIND_RESOURCE_BY_PAYLOAD",
    "SQL_FIND_RESOURCE_BY_SCHEDULE_KEY",
    "SQL_TASK_COUNTS",
    "SQL_TENANT_OF_RUN",
    "SQL_UPSERT_BLOCK",
    "SQL_UPSERT_GAP",
    "SQL_UPSERT_REGION",
    "SQL_UPSERT_RESOURCE",
    "SQL_UPSERT_STATE",
]

HOST_SLOT_KINDS: frozenset[TaskKind] = frozenset({TaskKind.FETCH, TaskKind.RENDER, TaskKind.UNLOCK})


SQL_INSERT_RUN = """
INSERT INTO runs (run_id, tenant_id, scope_hash, scope, profile, tool_versions, cost_ceiling_usd)
VALUES (%(run_id)s, %(tenant_id)s, %(scope_hash)s, %(scope)s::jsonb, %(profile)s,
        %(tool_versions)s::jsonb, %(cost_ceiling_usd)s)
ON CONFLICT (run_id) DO NOTHING
"""

SQL_ENSURE_HOST = """
INSERT INTO host_policy (tenant_id, host, tier_ceiling, max_concurrency)
VALUES (%(tenant_id)s, %(host)s, %(tier_ceiling)s, %(max_concurrency)s)
ON CONFLICT (tenant_id, host) DO NOTHING
"""

SQL_ENQUEUE = """
INSERT INTO tasks (run_id, tenant_id, kind, idempotency_key, host, priority, max_attempts,
                   not_before, payload)
VALUES (%(run_id)s, %(tenant_id)s, %(kind)s, %(idempotency_key)s, %(host)s, %(priority)s,
        %(max_attempts)s, COALESCE(%(not_before)s, now()), %(payload)s::jsonb)
ON CONFLICT (idempotency_key) DO NOTHING
RETURNING id
"""

# Lease a task that does not need a host slot (extraction, OCR, verification...).
SQL_LEASE_PLAIN = """
WITH candidate AS (
    SELECT t.id
    FROM tasks t
    JOIN runs r ON r.run_id = t.run_id
    WHERE t.state = 'PENDING'
      AND t.not_before <= now()
      AND t.kind = ANY(%(kinds)s)
      AND (%(run_id)s::text IS NULL OR t.run_id = %(run_id)s::text)
      AND NOT r.cancel_requested
    ORDER BY t.priority, t.id
    FOR UPDATE OF t SKIP LOCKED
    LIMIT 1
)
UPDATE tasks AS t
SET state = 'LEASED', lease_owner = %(worker)s, attempts = t.attempts + 1,
    lease_expires_at = now() + make_interval(secs => %(ttl_s)s), heartbeat_at = now()
FROM candidate
WHERE t.id = candidate.id
RETURNING t.*
"""

# Lease a task that consumes a host slot. The host row is locked with the task
# row; a locked host is skipped, so concurrency per host can never be exceeded.
SQL_LEASE_WITH_SLOT = """
WITH candidate AS (
    SELECT t.id AS task_id, h.tenant_id, h.host
    FROM tasks t
    JOIN runs r ON r.run_id = t.run_id
    JOIN host_policy h ON h.tenant_id = t.tenant_id AND h.host = t.host
    WHERE t.state = 'PENDING'
      AND t.not_before <= now()
      AND t.kind = ANY(%(kinds)s)
      AND (%(run_id)s::text IS NULL OR t.run_id = %(run_id)s::text)
      AND NOT r.cancel_requested
      AND NOT h.legal_hold
      AND (h.circuit_state <> 'OPEN' OR h.circuit_opened_at IS NULL
           OR h.circuit_opened_at <= now() - make_interval(secs => %(circuit_open_s)s))
      AND h.in_flight < h.max_concurrency
      AND h.next_allowed_at <= now()
      AND (h.retry_after_until IS NULL OR h.retry_after_until <= now())
    ORDER BY t.priority, t.id
    FOR UPDATE OF t, h SKIP LOCKED
    LIMIT 1
), slot AS (
    UPDATE host_policy h
    SET in_flight = h.in_flight + 1,
        next_allowed_at = now() + make_interval(secs => h.base_delay_ms / 1000.0),
        updated_at = now()
    FROM candidate c
    WHERE h.tenant_id = c.tenant_id AND h.host = c.host
    RETURNING h.host
)
UPDATE tasks AS t
SET state = 'LEASED', lease_owner = %(worker)s, attempts = t.attempts + 1,
    lease_expires_at = now() + make_interval(secs => %(ttl_s)s), heartbeat_at = now(),
    holds_host_slot = TRUE
FROM candidate c
WHERE t.id = c.task_id
RETURNING t.*
"""

SQL_HEARTBEAT = """
UPDATE tasks
SET heartbeat_at = now(), lease_expires_at = now() + make_interval(secs => %(ttl_s)s)
WHERE id = %(id)s AND lease_owner = %(worker)s AND state = 'LEASED'
"""

SQL_CHECKPOINT = """
UPDATE tasks
SET checkpoint = %(checkpoint)s::jsonb, heartbeat_at = now(),
    lease_expires_at = now() + make_interval(secs => %(ttl_s)s)
WHERE id = %(id)s AND lease_owner = %(worker)s AND state = 'LEASED'
"""

SQL_RELEASE_SLOT = """
UPDATE host_policy h
SET in_flight = GREATEST(0, h.in_flight - 1), updated_at = now()
FROM tasks t
WHERE t.id = %(id)s AND t.holds_host_slot
  AND h.tenant_id = t.tenant_id AND h.host = t.host
"""

SQL_COMPLETE = """
UPDATE tasks
SET state = 'DONE', finished_at = now(), lease_expires_at = NULL
WHERE id = %(id)s AND lease_owner = %(worker)s AND state = 'LEASED'
RETURNING id, holds_host_slot
"""

SQL_FAIL = """
UPDATE tasks
SET state = CASE WHEN %(retry)s AND attempts < max_attempts THEN 'PENDING' ELSE 'FAILED' END,
    last_error = %(error)s,
    not_before = CASE WHEN %(retry)s THEN COALESCE(%(not_before)s, now()) ELSE not_before END,
    lease_owner = NULL, lease_expires_at = NULL,
    finished_at = CASE WHEN %(retry)s AND attempts < max_attempts THEN NULL ELSE now() END
WHERE id = %(id)s AND lease_owner = %(worker)s AND state = 'LEASED'
RETURNING id, state, holds_host_slot
"""

# Reaper: expired leases go back to PENDING, or to QUARANTINED at max_attempts.
SQL_REAP = """
WITH expired AS (
    SELECT id FROM tasks
    WHERE state = 'LEASED' AND lease_expires_at < now()
    FOR UPDATE SKIP LOCKED
), released AS (
    UPDATE host_policy h
    SET in_flight = GREATEST(0, h.in_flight - 1), updated_at = now()
    FROM tasks t JOIN expired e ON e.id = t.id
    WHERE t.holds_host_slot AND h.tenant_id = t.tenant_id AND h.host = t.host
    RETURNING t.id
)
UPDATE tasks AS t
SET state = CASE WHEN t.attempts >= t.max_attempts THEN 'QUARANTINED' ELSE 'PENDING' END,
    last_error = COALESCE(t.last_error, 'lease_expired'),
    lease_owner = NULL, lease_expires_at = NULL, holds_host_slot = FALSE,
    finished_at = CASE WHEN t.attempts >= t.max_attempts THEN now() ELSE NULL END
FROM expired e
WHERE t.id = e.id
RETURNING t.id, t.state, t.run_id, t.kind, t.host, t.attempts, t.max_attempts, t.payload,
          t.last_error
"""

SQL_CANCEL_RUN = """
UPDATE runs SET cancel_requested = TRUE, state = 'CANCELLING' WHERE run_id = %(run_id)s
"""

SQL_CANCEL_OPEN_TASKS = """
UPDATE tasks
SET state = 'CANCELLED', last_error = %(reason)s, finished_at = now()
WHERE run_id = %(run_id)s AND state = 'PENDING' AND kind = ANY(%(kinds)s)
RETURNING *
"""

SQL_APPEND_EVENT = """
INSERT INTO events (run_id, entity_type, entity_id, old_state, new_state, inventory_revision,
                    reason, attempt, evidence_ids)
VALUES (%(run_id)s, %(entity_type)s, %(entity_id)s, %(old_state)s, %(new_state)s,
        %(inventory_revision)s, %(reason)s, %(attempt)s, %(evidence_ids)s::jsonb)
RETURNING seq
"""

SQL_PENDING_COUNT = """
SELECT count(*) AS n FROM tasks
WHERE kind = ANY(%(kinds)s) AND state IN ('PENDING', 'LEASED')
  AND (%(run_id)s::text IS NULL OR run_id = %(run_id)s::text)
"""

SQL_TASK_COUNTS = """
SELECT state, count(*) AS n FROM tasks WHERE run_id = %(run_id)s GROUP BY state
"""

SQL_UPSERT_RESOURCE = """
INSERT INTO resources (resource_id, run_id, schedule_key, requested_url, final_url, host,
                       mandatory, status, status_reason, payload_sha256, acquired_at_tier,
                       expected_mime, expected_mime_source, record)
VALUES (%(resource_id)s, %(run_id)s, %(schedule_key)s, %(requested_url)s, %(final_url)s, %(host)s,
        %(mandatory)s, %(status)s, %(status_reason)s, %(payload_sha256)s, %(acquired_at_tier)s,
        %(expected_mime)s, %(expected_mime_source)s, %(record)s::jsonb)
ON CONFLICT (run_id, schedule_key) DO UPDATE
SET final_url = EXCLUDED.final_url, status = EXCLUDED.status,
    status_reason = EXCLUDED.status_reason, payload_sha256 = EXCLUDED.payload_sha256,
    acquired_at_tier = EXCLUDED.acquired_at_tier,
    expected_mime = EXCLUDED.expected_mime,
    expected_mime_source = EXCLUDED.expected_mime_source,
    record = EXCLUDED.record, updated_at = now()
"""

SQL_UPSERT_BLOCK = """
INSERT INTO extractions (extraction_id, run_id, region_id, block_key, method, tool_version, record)
VALUES (%(extraction_id)s, %(run_id)s, %(region_id)s, %(block_key)s, %(method)s, %(tool_version)s,
        %(record)s::jsonb)
ON CONFLICT (run_id, region_id, method) DO UPDATE
SET block_key = EXCLUDED.block_key, tool_version = EXCLUDED.tool_version, record = EXCLUDED.record
"""

SQL_GET_RESOURCE = """
SELECT record FROM resources WHERE resource_id = %(resource_id)s
"""

SQL_GET_BLOCKS = """
SELECT e.record FROM extractions e JOIN resources r ON r.run_id = e.run_id
WHERE r.resource_id = %(resource_id)s AND e.region_id LIKE %(prefix)s
ORDER BY e.extraction_id
"""

SQL_RECORD_SUMMARY = """
UPDATE resources SET record = record || jsonb_build_object(%(key)s, %(value)s::jsonb),
    updated_at = now()
WHERE resource_id = %(resource_id)s
"""

SQL_TENANT_OF_RUN = """
SELECT tenant_id FROM runs WHERE run_id = %(run_id)s
"""

SQL_UPSERT_GAP = """
INSERT INTO gaps (gap_id, run_id, resource_id, gap_type, status, record)
VALUES (%(gap_id)s, %(run_id)s, %(resource_id)s, %(gap_type)s, %(status)s, %(record)s::jsonb)
ON CONFLICT (gap_id) DO UPDATE SET status = EXCLUDED.status, record = EXCLUDED.record
"""

SQL_GET_GAPS = """
SELECT record FROM gaps WHERE resource_id = %(resource_id)s ORDER BY created_at, gap_id
"""

SQL_RESOURCE_COUNTS = """
SELECT status, count(*) AS n FROM resources WHERE run_id = %(run_id)s GROUP BY status
"""

SQL_LOAD_HOST = """
SELECT host, access_tier, tier_ceiling, circuit_state, circuit_opened_at, consecutive_failures,
       clean_streak, block_score, session_generation, robots_cache, robots_fetched_at,
       crawl_delay_ms
FROM host_policy WHERE tenant_id = %(tenant_id)s AND host = %(host)s
"""

SQL_SAVE_HOST = """
UPDATE host_policy
SET access_tier = %(access_tier)s, circuit_state = %(circuit_state)s,
    circuit_opened_at = %(circuit_opened_at)s, consecutive_failures = %(consecutive_failures)s,
    clean_streak = %(clean_streak)s, block_score = %(block_score)s,
    session_generation = %(session_generation)s,
    retry_after_until = COALESCE(%(retry_after_until)s, retry_after_until),
    updated_at = now()
WHERE tenant_id = %(tenant_id)s AND host = %(host)s
"""

SQL_SET_ROBOTS = """
UPDATE host_policy
SET robots_cache = %(robots_cache)s, robots_fetched_at = %(fetched_at)s,
    crawl_delay_ms = COALESCE(%(crawl_delay_ms)s, crawl_delay_ms),
    base_delay_ms = GREATEST(base_delay_ms, COALESCE(%(crawl_delay_ms)s, 0)),
    updated_at = now()
WHERE tenant_id = %(tenant_id)s AND host = %(host)s
"""

SQL_RECORD_EDGE = """
INSERT INTO resource_edges (run_id, parent_id, child_id, edge_kind, evidence_id)
VALUES (%(run_id)s, %(parent_id)s, %(child_id)s, %(edge_kind)s, %(evidence_id)s)
ON CONFLICT (run_id, parent_id, child_id, edge_kind) DO UPDATE
SET evidence_id = COALESCE(EXCLUDED.evidence_id, resource_edges.evidence_id)
"""

SQL_FIND_RESOURCE_BY_SCHEDULE_KEY = """
SELECT record FROM resources WHERE run_id = %(run_id)s AND schedule_key = %(schedule_key)s
"""

SQL_FIND_RESOURCE_BY_PAYLOAD = """
SELECT record FROM resources
WHERE run_id = %(run_id)s AND payload_sha256 = %(payload_sha256)s
LIMIT 1
"""

SQL_UPSERT_STATE = """
INSERT INTO states (state_id, run_id, resource_id, parent_state_id, state_key, status, record)
VALUES (%(state_id)s, %(run_id)s, %(resource_id)s, %(parent_state_id)s, %(state_key)s,
        %(status)s, %(record)s::jsonb)
ON CONFLICT (run_id, resource_id, state_key) DO UPDATE
SET parent_state_id = EXCLUDED.parent_state_id, status = EXCLUDED.status, record = EXCLUDED.record
"""

SQL_GET_STATES = """
SELECT record FROM states
WHERE run_id = %(run_id)s AND resource_id = %(resource_id)s
ORDER BY state_id
"""

SQL_UPSERT_REGION = """
INSERT INTO regions (region_id, run_id, resource_id, state_id, region_key, status, record)
VALUES (%(region_id)s, %(run_id)s, %(resource_id)s, %(state_id)s, %(region_key)s,
        %(status)s, %(record)s::jsonb)
ON CONFLICT (run_id, resource_id, state_id, region_key) DO UPDATE
SET status = EXCLUDED.status, record = EXCLUDED.record
"""

SQL_GET_REGIONS = """
SELECT record FROM regions
WHERE run_id = %(run_id)s AND resource_id = %(resource_id)s
ORDER BY region_id
"""

SQL_LIST_RESOURCES = """
SELECT record FROM resources WHERE run_id = %(run_id)s ORDER BY schedule_key
"""

SQL_GET_EVENTS_FOR_RUN = """
SELECT seq, run_id, entity_type, entity_id, old_state, new_state, inventory_revision,
       reason, attempt, evidence_ids, at
FROM events
WHERE run_id = %(run_id)s
ORDER BY seq
"""

SQL_GET_EVENTS_FOR_ENTITY = """
SELECT seq, run_id, entity_type, entity_id, old_state, new_state, inventory_revision,
       reason, attempt, evidence_ids, at
FROM events
WHERE run_id = %(run_id)s AND entity_id = %(entity_id)s
ORDER BY seq
"""

SQL_OUTSTANDING_RESOURCE_IDS = """
SELECT DISTINCT res_id FROM (
    SELECT t.payload->>'resource_id' AS res_id
    FROM tasks t
    WHERE t.run_id = %(run_id)s
      AND t.state IN ('PENDING', 'LEASED')
      AND t.kind = ANY(%(kinds)s)
      AND t.payload ? 'resource_id'
    UNION
    SELECT r.resource_id AS res_id
    FROM tasks t
    JOIN resources r ON r.run_id = t.run_id AND r.schedule_key = (t.payload->>'schedule_key')
    WHERE t.run_id = %(run_id)s
      AND t.state IN ('PENDING', 'LEASED')
      AND t.kind = ANY(%(kinds)s)
      AND t.payload ? 'schedule_key'
) sub
WHERE res_id IS NOT NULL AND res_id <> ''
"""
