# Module: Read-only MCP server (Claude Desktop) and "Open in Claude.ai"

Status: **built 2026-11-03** on `feat/mcp-server` (`src/modules/mcp_server/`). Step 6 of the roadmap in `MODULE_ANALYST_ACTION_CARDS.md`. Uses your Claude Desktop or claude.ai login; no API key, no API cost in the app.

**Data policy first.** Everything a tool returns (page text, keywords, findings) goes to Anthropic through your Claude account, exactly as with the API. Which terms apply depends on the account (Team/Enterprise: commercial terms; Pro/Max: consumer terms and your training setting). Use it only for clients whose data may go to Claude.

## How it fits together

```
Claude Desktop --stdio--> python -m src.modules.mcp_server --HTTP 127.0.0.1--> web app (POST /api/mcp/call)
                                                                                  |
                                              tools run here; the web app is the only writer of app data
```

- The stdio server never reads or writes app data; it forwards each call to the running web app. When the app is not running, every tool answers how to start it.
- `POST /api/mcp/call` accepts only the app's header, JSON, and **no Origin** (no web page, not even the app's own, can call it). The app binds 127.0.0.1 only.
- The tools are read-only towards websites and project data: no scrape, no Semrush pull, no status change, no unlock. Some build a missing local result on first use (theme map, gap report, monitoring check, SEO inventory); that runs inside the app, on this computer, at $0. The one tool that writes, `save_deliverable`, only adds a checked answer to the project's analyst run history (below).

## Tools (24 in the full connector)

| Tool | What |
|---|---|
| `list_projects` | Projects, client, websites and roles, which results exist with dates. Call first. |
| 17 analyst tools | `list_websites`, `search_knowledge`, `get_page`, `get_page_facts`, `list_pages`, `get_offering_catalog`, `get_seo_issues`, `find_similar_pages`, `get_changes`, `get_theme_map`, `get_monitoring_alerts`, `get_gap_findings`, `get_keyword_rankings`, `get_serp_overview`, `get_keyword_metrics`, `get_search_demand`, `get_competitor_suggestions`; each takes `project_id`. Results are marked as untrusted website content and capped in size. |
| `get_theme_pages`, `export_theme_pages_csv` | Every page of one theme, in full, as a list or as CSV text (answered by the stdio server from the app's read-only endpoints). |
| `get_scrape_queue` | What is running, waiting and ready across all projects, with progress. |
| `present_answer` | A structured answer laid out by type (below). |
| `check_deliverable` | Checks an analysis by code: every `[chunk:...]`/`[fact:...]` citation must be one a tool returned for this project, quotes must match word for word, facts must be cited. Returns passed / needs review, the problems and a Sources table. |
| `save_deliverable` | `(project_id, title, markdown, card_key?, module?)`: runs the same checks as `check_deliverable`, then keeps the answer in the project's analyst run history as a run of kind `claude_desktop` ("Written in Claude Desktop"). Saving the same title again for the same project adds a **version** to the same run, not a second entry. The only tool that writes; it writes through the web app, like everything else. |

The server sends Claude the analyst rules (evidence only, exact quotes, untrusted content, no invented numbers) and asks it to call `check_deliverable` before answering, then `save_deliverable` once the check passes. References are kept per project in `app_data/mcp/ledgers/<project>.json`, so a check still works after Claude Desktop or the app restarts.

## Saving answers into the app (`save_deliverable`)

An answer written in Claude Desktop used to live only in the chat. `save_deliverable` hands it back to the app (`POST /api/mcp/call`, like every tool; the web app stays the only writer of app data), which:

1. checks it exactly as `check_deliverable` does (citations known for this project, quotes verbatim, facts cited); the result never blocks the save, so a draft that needs review is kept with that result and can be fixed and saved again;
2. stores it under `app_data/analyst/runs/<run>/` as a run with `backend: claude_desktop` and `status: finished`: `deliverable.vN.md` (the readable text with numbered sources, then the "Verification (by code)" and "Sources" sections), `draft.vN.md` (the text as Claude sent it, with its reference codes), `ledger.json` (the cited references), `events.jsonl`; `run.json` carries the `title`, `author` ("Claude Desktop", or "Claude Desktop (Themes)" on a module connector), the time, `module` (the argument, or the connector's module when it has one), `verification`, `version` and the list of `versions` (number, time, check, files); `card_key` names an action card when the user asked for one, otherwise the card id is `claude-desktop`;
3. answers with the status, run id, version, author, time, the check's details, `readable_markdown` and `sources_markdown`, and tells Claude to say in one line that the answer is saved in the Analyst cards tab.

**Versions.** The same title for the same project (case and spacing aside) is one run: each save adds a version, the first spelling of the title is kept, and every version's files stay. The app lists the run once with its number of versions; the viewer opens the latest and can show an earlier one (`GET .../analyst/runs/{run}?version=N`).

**Confidential projects are closed entirely.** Everything a tool returns reaches Anthropic, and names, titles, themes and findings describe the client too, so for a project marked confidential (the app's option or `client_confidential` in `data_policy.json`) no tool runs, saving included, and `list_projects` leaves it out (`hidden_projects` gives the number). A project whose websites are read from a confidential project's scrape (the freshest copy of a shared website) is closed the same way, without naming that project. `get_theme_pages`, `export_theme_pages_csv` and `get_scrape_queue`, answered by the stdio server itself, ask `GET /api/mcp/blocked` first and answer nothing when the app cannot say.

**In the app.** The Analyst cards tab lists saved deliverables with the card runs, labelled "Written in Claude Desktop", with title, author, saved time, the check and the versions; they open to the readable Markdown with the check result and sources, download as Markdown or Word, and the Downloads module's analyst report has a "Written in Claude Desktop" table and, in Word, the latest version of each deliverable in full.

**Search without the local model.** When Ollama is not reachable, search now falls back to exact words (instead of returning nothing) and says so in a note; the app's search tab shows the same note.

## Readable answers

`check_deliverable` also returns `readable_markdown`: the answer as the user should see it, with every `[chunk:...]`/`[fact:...]` replaced by a number (passages of one page share a number, neighbouring citations merge into `[1, 2]`) and a **Sources** list of page names and links (a theme links its main page). The instructions tell Claude to show only that version, never reference codes, internal ids, tool names or JSON fields, to name themes by `name`, and to end with one plain line on the check. `get_theme_map` returns each theme's `name` (the cleaned title of its main page, the client's first; keyword label when no title) and `keywords`.

## Answer templates and ready-made requests

`present_answer` (in every connector) takes a structured answer: `type` (answer, overview, findings, comparison, page_review, changes, keywords), `title`, `summary`, and optional `sections`, `table` (columns and rows), `items` (title, detail, priority high/medium/low, metric), `next_steps`, `limits`, every fact with its reference. The app checks the facts (`verify_deliverable` on the content alone, without the template) and renders Markdown (`templates.render_answer`): `## <icon> <title>`, project, type and date line, an "In short" quote, the type's layout (findings grouped by priority; page review split into what works and what to fix; overview, comparison and keywords lead with the table; changes as a list with priority icons), next steps, limits, a "Sources checked" or "Needs review" line, and numbered linked sources. Claude is told to show `rendered_markdown` exactly as returned and to fix and resend when it needs review.

Prompts (Claude Desktop's prompt menu, per connector scope): Ask the pages, Page review (content); Theme overview, Compare with competitors (themes); What changed (monitoring); Keyword report (research); Gap report (gaps). Each takes the project (optional) and its own argument, and asks for the matching tool calls and answer type.

## Module scope: one module per chat, without the others

A connection can be limited to some modules, so a chat that should stay within one module never sees the others:

| Module key | Tools |
|---|---|
| `content` | list_websites, search_knowledge, get_page, get_page_facts, list_pages, get_seo_issues, find_similar_pages |
| `themes` | get_offering_catalog, get_theme_map |
| `monitoring` | get_changes, get_monitoring_alerts |
| `research` | get_keyword_rankings, get_serp_overview, get_keyword_metrics, get_search_demand |
| `gaps` | get_gap_findings |

`list_projects`, `present_answer`, `check_deliverable` and `save_deliverable` are in every connection; on a module connector `save_deliverable` records the module in the author and refuses a `module` argument outside the connection.

- **One connector per module.** `python -m src.modules.mcp_server --print-config --every-module` prints the full connector plus `content-processor-content`, `-themes`, `-monitoring`, `-research` and `-gaps` (each started with `--modules <key>`). In a Claude Desktop chat, switch on only the connector you want: the other modules' tools never enter that conversation.
- **What a scoped connection does:** lists only its modules' tools; tells Claude it is limited to them and must say which module a question needs instead of looking elsewhere; `list_projects` shows only those modules' state; and the web app refuses any other module's tool on it (`"this connection is limited to Themes; get_gap_findings belongs to Gaps"`), so the limit holds even if a tool were called directly.
- **The full connector** asks Claude to respect "only use Themes" in a chat: every tool description starts with its module (`[Themes] ...`), and the instructions say to call only that module's tools then. That is an instruction, not enforced; for a strict limit use a module connector.
- **Why not a switch Claude turns on in a chat:** Claude Desktop runs one connector process for all chats, so a switch set in one chat would apply to every other chat too.
- A module's own results may name what they were built from (a gap finding names its theme); that is the module's output, not a look-up in another module.

## Setup (Windows)

1. `pip install "mcp>=2.3,<3"` (or `pip install -e ".[mcp]"`).
2. Start the app: `python -m src.modules.content_processor.webapi --exports exports --port 8780`.
3. Print the config entry with full paths: `python -m src.modules.mcp_server --print-config`. On this computer (main checkout):

```json
{
  "mcpServers": {
    "content-processor": {
      "command": "C:\\Users\\RankUno\\AppData\\Local\\Programs\\Python311\\python.exe",
      "args": ["-m", "src.modules.mcp_server", "--app-url", "http://127.0.0.1:8780"],
      "env": {
        "PYTHONPATH": "C:\\Users\\RankUno\\Desktop\\content-processor",
        "PYTHONUTF8": "1"
      }
    }
  }
}
```

4. In Claude Desktop: Settings → Developer → Edit Config, paste the `content-processor` entry inside `mcpServers` (keep any servers already there), save, and restart Claude Desktop completely. The Microsoft Store version keeps the file inside its package folder; Edit Config opens the right one.
5. Restart Claude Desktop again whenever the tools change.

## "Open in Claude.ai" (web app)

On a context pack in the Analyst cards viewer: copies the pack, then opens `https://claude.ai/new` in a new tab (no link back to the app). Paste with Ctrl+V and send. One way only: the answer stays in claude.ai and is neither checked nor saved by the app; it uses whichever Claude account is signed in to that browser.

## Edge cases handled

| Case | Handling |
|---|---|
| App not running | Each tool answers how to start it |
| Missing, unknown or malformed `project_id` | Error naming `list_projects` |
| Unknown or write tool (`start_scrape`) | Refused; not in the list (`save_deliverable` is the one write, into the run history only) |
| Invalid arguments | The tool's own validation message |
| A tool fails | Error text, logged in the app; the server keeps running |
| Two processes writing app data | Only the web app writes; ledger saved under a lock through a unique temporary file; saves are serialised, so two saves of one title get versions 1 and 2 |
| `save_deliverable` on an app started without the analyst module | Answered with how to start the app with its projects; nothing written |
| A saved answer that fails the check | Saved as "needs review" with the problems; saving again with the same title adds a corrected version |
| A web page posting to the bridge | Refused (Origin present) |
| `--app-url` not on this computer | Refused (`localhost.evil.com` too) |
| Stray output on stdout | The MCP library redirects it away from the protocol; the app logs to stderr |
| Ollama down | Search by exact words, with a note |
| Large results | Halved until under the size limit, with a note |

## Not done yet

- Write tools with confirmation in the app (statuses first; scrapes and Semrush only with explicit approval).
- claude.ai (browser) connector: needs the app hosted with sign-in.
