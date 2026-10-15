# ADR-0001: Postgres-backed task ledger instead of Celery + Redis

**Status**: Proposed · **Date**: 2026-10-14

## Context
The unified investigation proposed Celery + Redis. A 5,000-URL run must survive worker, browser and host crashes without losing or duplicating work, and must checkpoint long tasks (state exploration, multi-page OCR). Postgres already runs on the Rankuno server.

## Decision
Tasks are rows in Postgres. Workers lease with `SELECT … FOR UPDATE SKIP LOCKED`, heartbeat by extending `lease_expires_at`, checkpoint into `payload.checkpoint`, and finish with a state transition. A reaper reclaims expired leases and quarantines tasks at `max_attempts`. Idempotency keys make re-delivery a no-op upsert.

## Consequences
- One durable system for queue, ledger, governor and events; transactional consistency between "task done" and "ledger updated".
- No broker to operate; no at-most-once/at-least-once ambiguity.
- Throughput ceiling is far above need (~110k tasks per 5,000-URL run). If a future workload exceeds ~1k leases/s, migrate the lease table to NATS JetStream or RabbitMQ behind the same `TaskRepository` interface.
- Playwright's asyncio loop runs in a plain worker process; no Celery prefork interaction.
