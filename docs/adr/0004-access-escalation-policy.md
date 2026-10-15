# ADR-0004: Tiered access escalation bounded by a per-run ceiling

**Status**: Proposed · **Date**: 2026-10-14

## Context
The problem statement requires maximum access; the investigation corpus requires honest blocked outcomes and forbids making evasion a prerequisite. Rankuno runs for both owner-authorized client sites and public third-party sites.

## Decision
Access is a ladder T0 (authorized) → T1 (polite HTTP) → T2 (impersonated HTTP) → T3 (fidelity browser) → T4 (proxies, FINANCIAL) → T5 (managed unblockers, FINANCIAL + HITL) → T6 (policy-gated, off). Every run declares `access_tier_ceiling` derived from its `authorization`. The host governor escalates per host on classified blocks and never beyond the ceiling. Every resource records the tier that acquired it. Client sites default to T0 with allow-listing and a self-identifying UA.

## Consequences
- "Not blocked" is maximized within policy; "blocked" stays visible with evidence and the tier reached.
- Paid tiers are governed by `CostLedger` and Step 3 approval, matching Rankuno's FINANCIAL rules.
- The block classifier runs on every response at every tier, including vendor-returned ones.
