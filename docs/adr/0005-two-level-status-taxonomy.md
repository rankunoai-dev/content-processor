# ADR-0005: Two-level status taxonomy and typed gaps

**Status**: Proposed · **Date**: 2026-10-14

## Context
The unified investigation mixed three status vocabularies (badge table, gap example, block example).

## Decision
Entities (resource, state, region, task) carry `EntityStatus` (pending: DISCOVERED, QUEUED, IN_PROGRESS, RETRY_SCHEDULED; terminal: VERIFIED_EXTRACTED, EXTRACTED_UNVERIFIED, PARTIAL, BLOCKED, FAILED, UNSUPPORTED, BUDGET_EXHAUSTED, EXCLUDED_BY_POLICY, VERIFIED_NONCONTENT). URLs and runs carry `UrlBadge` derived by precedence (RUNNING, FAILED, BLOCKED, PARTIAL, NEEDS_REVIEW, VERIFIED_WITHIN_SCOPE, EXCLUDED) and always ship with the full status vector. Gaps carry a `GapType` enum (14 values) and evidence ids.

## Consequences
- Contracts are typable as StrEnums in `contracts.py`; CI rejects unknown values.
- A badge can never absorb a gap; the vector and gap list are mandatory fields of `UrlRecord`.
