# Downloads (every module's data as Excel, CSV, Word, PDF or JSON)

`src/modules/reports/`. Every report is built on request from the modules' latest saved data (no scrape, pull, analysis or cost) into a temporary folder that is removed once the file is sent. Reading needs no unlock, like the tabs.

## Reports (`catalog.py`, `REPORTS`)

| Key | Module | From | Missing until |
|---|---|---|---|
| `overview` | content | the project's websites (side, addresses, note, tags, list size, last scrape) and every scrape | always available |
| `themes` | themes | `theme_tables(latest map)` | a theme map is built |
| `monitoring` | monitoring | `check_tables(latest check)`: Alerts, Websites, Changed pages, New and removed pages, Theme drift, Competitor moves, Price changes, Template changes, How to read; JSON adds the whole check as `data` | a check is run |
| `monitoring_history` | monitoring | every saved check (latest 200), oldest first: Checks (alerts by severity, new/removed/changed pages per check), All alerts (newest first), Recurring alerts (kind + website + severity: checks raised, first and last seen) | a check is run |
| `research` | research | `research_tables(latest snapshot, movers vs previous)` | research is imported or pulled |
| `competitors` | research | the competitor book (status, sources, relevance, overlap, traffic, top 10, examples, seen) | a suggestion exists |
| `gaps` | gaps | `gap_tables(latest report)` | the gap analysis is built |
| `analyst` | analyst | the project's card runs (card, backend, status, times, cost, check, inputs, message) | a run exists (and the app has the analyst runner) |

Each report is a `Report` (title, subtitle with when its data was made, summary lines, tables, and optionally `data`: the module's own record, JSON-ready).

## Formats (`writers.py`)

- `xlsx`: the shared writer (`export/tabular.write_xlsx`): nothing cut; long cells split, long tables over several sheets.
- `csv`: one table -> `.csv`; several -> `.zip` with `NN_<table>.csv` (UTF-8 BOM, formula guard).
- `docx` (python-docx, the `office` extra; not offered without it): title, subtitle, summary bullets, each table with a bold header; first `WORD_ROWS` (300) rows and `WORD_CELL` (600) characters per cell, with a note pointing to Excel; landscape when a table has more than 6 columns.
- `pdf` (reportlab, the `pdf` extra; not offered without it): the same document as Word (title, subtitle, summary bullets, each table with the same `WORD_ROWS` / `WORD_CELL` limits and the same "see the Excel file" notes, id columns left out), A4, landscape when a table has more than 6 columns, the header row repeated on every page, a footer with the title and "Page N". Tables are laid out row by row (one small paragraph per cell, `LongTable`), never as one string. The built-in font (Helvetica) covers Latin scripts only: a character it lacks is shown as `?` and a note at the end of the table says how many were replaced and points to Excel; nothing crashes. `CP_PDF_FONT` (and `CP_PDF_FONT_BOLD`) name a TrueType file with wider coverage (Noto, DejaVu), registered once per process.
- `json`: `{report, title, subtitle, generated_at, summary, tables: [{name, columns, rows}], data?}`; every row and the full text, nothing shortened.
- `markdown_to_docx` / `markdown_to_pdf`: analyst results (headings, paragraphs, bullet and numbered lists, tables, quotes, code, bold, links as "text (url)"), from one shared block parser (`_markdown_blocks`).

## API

- `GET /api/projects/{id}/exports`: `formats` (only those this installation can write: `docx` needs python-docx, `pdf` reportlab) and every report with `available`, `missing` (what to do first), `subtitle`, `rows`.
- `GET /api/projects/{id}/exports/{key}.{xlsx|csv|docx|pdf|json}`: one report; 404 with the "missing" text when not ready.
- `GET /api/projects/{id}/exports/all.zip`: every ready report in every format, plus `README.txt` (what each file is, what is not included yet and why).
- `GET /api/projects/{id}/analyst/runs/{run}/result/result.{md|docx|pdf}`: a card run's deliverable or context pack.

File names: `<project>_<key>_<YYYY-MM-DD>.<ext>`.

## App

- `components/DownloadMenu.tsx`: the Download button (Excel / CSV / Word / PDF / JSON; optional `label`) in Themes, Monitoring, Research, Gaps and Suggested competitors (replaces the Excel-only workbook buttons; the workbook routes remain).
- `routes/DownloadsTab.tsx`: the project's Downloads tab (every report, its formats or what it needs, Download everything).
- Monitoring tab: **Download** (latest check) and **Download history** (`monitoring_history`).
- Analyst result viewer: Word and PDF next to the Markdown download.

## Not done yet

- Knowledge search results and the theme/monitoring comparisons as downloads.
- PDF text in non-Latin scripts without a font file: the built-in font has no Devanagari, Tamil, Arabic, Chinese or Japanese glyphs, so those characters show as `?` (with a note) until `CP_PDF_FONT` points at a TrueType font that has them. Bold inside a PDF built from a `CP_PDF_FONT` file needs `CP_PDF_FONT_BOLD`, else it is shown in the regular face. The PDF has no clickable links (links appear as "text (url)", as in Word) and no table of contents.
