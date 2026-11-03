# Project instructions for every session

## Keep the plain-language guide current

`Content_Processor_Guide.docx` (project root) explains the whole engine to a non-technical reader. Its source of truth is `docs/guide/GUIDE.md`.

Whenever a session lands a significant feature (a new capability, check, report or adapter; a change to safety, cost or access rules; or new headline results), in the same change:

1. Update `docs/guide/GUIDE.md` in plain, non-technical language: the relevant section, the "Last updated" line, and a new change-log row.
2. Rebuild the Word file: `python scripts/build_guide.py`.
3. Commit both files with the feature.

Bug fixes and refactors that change nothing a reader would notice do not need an entry.

## Coordination

Read `docs/LANE_LOG.md`, `docs/WORK_BREAKDOWN.md` and `docs/CONTRACT_REQUESTS.md` before starting, and append a lane-log entry when done.
