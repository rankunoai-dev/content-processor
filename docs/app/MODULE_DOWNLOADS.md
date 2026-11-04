# Downloads (every module's data as Excel, CSV or Word)

`src/modules/reports/`. Every report is built on request from the modules' latest saved data (no scrape, pull, analysis or cost) into a temporary folder that is removed once the file is sent. Reading needs no unlock, like the tabs.

## Reports (`catalog.py`, `REPORTS`)

| Key | Module | From | Missing until |
|---|---|---|---|
| `overview` | content | the project's websites (side, addresses, note, tags, list size, last scrape) and every scrape | always available |
| `themes` | themes | `theme_tables(latest map)` | a theme map is built |
| `monitoring` | monitoring | `check_tables(latest check)` | a check is run |
| `research` | research | `research_tables(latest snapshot, movers vs previous)` | research is imported or pulled |
| `competitors` | research | the competitor book (status, sources, relevance, overlap, traffic, top 10, examples, seen) | a suggestion exists |
| `gaps` | gaps | `gap_tables(latest report)` | the gap analysis is built |
| `analyst` | analyst | the project's card runs (card, backend, status, times, cost, check, inputs, message) | a run exists (and the app has the analyst runner) |

Each report is a `Report` (title, subtitle with when its data was made, summary lines, tables).

## Formats (`writers.py`)

- `xlsx`: the shared writer (`export/tabular.write_xlsx`): nothing cut; long cells split, long tables over several sheets.
- `csv`: one table -> `.csv`; several -> `.zip` with `NN_<table>.csv` (UTF-8 BOM, formula guard).
- `docx` (python-docx, the `office` extra; not offered without it): title, subtitle, summary bullets, each table with a bold header; first `WORD_ROWS` (300) rows and `WORD_CELL` (600) characters per cell, with a note pointing to Excel; landscape when a table has more than 6 columns.
- `markdown_to_docx`: analyst results (headings, paragraphs, bullet and numbered lists, tables, quotes, code, bold, links as "text (url)").

## API

- `GET /api/projects/{id}/exports`: `formats` and every report with `available`, `missing` (what to do first), `subtitle`, `rows`.
- `GET /api/projects/{id}/exports/{key}.{xlsx|csv|docx}`: one report; 404 with the "missing" text when not ready.
- `GET /api/projects/{id}/exports/all.zip`: every ready report in every format, plus `README.txt` (what each file is, what is not included yet and why).
- `GET /api/projects/{id}/analyst/runs/{run}/result/result.{md|docx}`: a card run's deliverable or context pack.

File names: `<project>_<key>_<YYYY-MM-DD>.<ext>`.

## App

- `components/DownloadMenu.tsx`: the Download button (Excel / CSV / Word) in Themes, Monitoring, Research, Gaps and Suggested competitors (replaces the Excel-only workbook buttons; the workbook routes remain).
- `routes/DownloadsTab.tsx`: the project's Downloads tab (every report, its formats or what it needs, Download everything).
- Analyst result viewer: Word next to the Markdown download.

## Not done yet

- Knowledge search results and the theme/monitoring comparisons as downloads.
- PDF.
