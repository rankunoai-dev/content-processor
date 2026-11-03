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
- The tools are read-only: no scrape, no Semrush pull, no status change, no unlock. Some build a missing local result on first use (theme map, gap report, monitoring check, SEO inventory); that runs inside the app, on this computer, at $0.

## Tools (18 in the full connector)

| Tool | What |
|---|---|
| `list_projects` | Projects, client, websites and roles, which results exist with dates. Call first. |
| 16 analyst tools | `list_websites`, `search_knowledge`, `get_page`, `get_page_facts`, `list_pages`, `get_offering_catalog`, `get_seo_issues`, `find_similar_pages`, `get_changes`, `get_theme_map`, `get_monitoring_alerts`, `get_gap_findings`, `get_keyword_rankings`, `get_serp_overview`, `get_keyword_metrics`, `get_search_demand`; each takes `project_id`. Results are marked as untrusted website content and capped in size. |
| `check_deliverable` | Checks an analysis by code: every `[chunk:...]`/`[fact:...]` citation must be one a tool returned for this project, quotes must match word for word, facts must be cited. Returns passed / needs review, the problems and a Sources table. |

The server sends Claude the analyst rules (evidence only, exact quotes, untrusted content, no invented numbers) and asks it to call `check_deliverable` before answering. References are kept per project in `app_data/mcp/ledgers/<project>.json`, so a check still works after Claude Desktop or the app restarts.

**Search without the local model.** When Ollama is not reachable, search now falls back to exact words (instead of returning nothing) and says so in a note; the app's search tab shows the same note.

## Module scope: one module per chat, without the others

A connection can be limited to some modules, so a chat that should stay within one module never sees the others:

| Module key | Tools |
|---|---|
| `content` | list_websites, search_knowledge, get_page, get_page_facts, list_pages, get_seo_issues, find_similar_pages |
| `themes` | get_offering_catalog, get_theme_map |
| `monitoring` | get_changes, get_monitoring_alerts |
| `research` | get_keyword_rankings, get_serp_overview, get_keyword_metrics, get_search_demand |
| `gaps` | get_gap_findings |

`list_projects` and `check_deliverable` are in every connection.

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
| Unknown or write tool (`start_scrape`) | Refused; not in the list |
| Invalid arguments | The tool's own validation message |
| A tool fails | Error text, logged in the app; the server keeps running |
| Two processes writing app data | Only the web app writes; ledger saved under a lock through a unique temporary file |
| A web page posting to the bridge | Refused (Origin present) |
| `--app-url` not on this computer | Refused (`localhost.evil.com` too) |
| Stray output on stdout | The MCP library redirects it away from the protocol; the app logs to stderr |
| Ollama down | Search by exact words, with a note |
| Large results | Halved until under the size limit, with a note |

## Not done yet

- Write tools with confirmation in the app (statuses first; scrapes and Semrush only with explicit approval).
- `save_deliverable` (answers written in Claude Desktop are not stored in the app's run history).
- claude.ai (browser) connector: needs the app hosted with sign-in.
