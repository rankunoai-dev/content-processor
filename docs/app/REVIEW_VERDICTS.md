# Reviewing verdicts of project scrapes

`src/modules/content_processor/projects/verdicts.py`, the project page's **Review** tab.

## Decisions

The scrape's decision log (`<run>.decisions.jsonl`, `export/decisions.py`) gains the kind `verdict`, keyed by the page's requested URL: `approved`, or `rejected` with a reason (`note`, required) and a `next_step` (required): `scrape_again`, `obtain_manually` or `exclude`. An empty decision clears. Lines are appended (history); the latest counts. The review queue reads verdicts as actions (approved = accept_as_is, scrape_again = retry_later, obtain_manually, exclude); a later `review` line still wins.

## Pages

From the scrape's page stamps (`<run>.stamps.jsonl`): URL, title, badge, gap types, one row per URL. `is_page`: an HTML, PDF or Office document that is not an asset, tracking pixel or policy exclusion (the rest shows with "Include files and images"). Counts per badge and decision, and the undecided pages.

## Follow-ups

- `scrape_again`: `ScrapeRunner.submit` writes `force_fetch.json` with the pages sent back in the website's latest finished scrape (`rescrape_urls`); `cli dryrun --force-fetch`, `Baseline.force_fresh` / `forced`; the fetch worker then looks up no prior capture for them (no conditional request, no reuse) and a planned skip never applies to them (they are also removed from `skip_fetch.json`).
- `exclude`: `project_reports._load` skips those pages (`excluded_urls`), and with one client website the scrape's own workbook is not copied while any page is excluded (it is rebuilt without them).
- `obtain_manually`: listed (filter "Decided"); no automatic step.

## API

`GET /api/projects/{id}/sites/{site}/runs/{run}/verdicts`; `POST` the same with `{url, decision, next_step, note}` (and `reviewer` without accounts). With accounts the gate requires reviewer or above (`access.REVIEW_PATHS`) and the reviewer is the account's display name; without accounts the project's unlock token and a name are required. 404 for a page not in the scrape, 422 for a rejection without reason or next step.

## Not done yet

- A list of "get it by hand" tasks across scrapes.
- Rebuilding a finished scrape's own reports after decisions (they read the log when they are built).
