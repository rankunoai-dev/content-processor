# ADR-0002: One browser per worker process, contexts per task, aggressive recycling

**Status**: Proposed · **Date**: 2026-10-14

## Context
Chromium leaks memory over long sessions and a renderer crash can take down sibling pages. The shared Rankuno server has a hard memory ceiling and Screaming Frog is P0.

## Decision
Each browser-worker process owns exactly one browser (real Chrome channel) with C contexts. Each task gets a fresh context unless the host session profile requires persistence. The browser is restarted every 50 pages, after 30 minutes, or when RSS exceeds 2.2 GB. The container has a 3 GB cgroup cap. Browser subrequests go through the egress proxy with the session's IP. Screenshots are JPEG; full-page captures once per state; crops for gaps.

## Consequences
- A crash loses at most one lease, which the reaper reclaims; the task checkpoint resumes at the last state.
- Predictable memory; the crawler can be throttled to one context without redeploy.
- Anti-detection fork (`patchright` or `camoufox`) is chosen in Stage 1 by fixture evaluation and pinned; hand-rolled stealth patches are not maintained in-repo.
