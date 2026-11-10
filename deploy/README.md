# Deploying the content processor (Railway or any container host)

The `Dockerfile` at the repository root builds one image with the web app server, the
scrape workers (Playwright's bundled Chromium, Tesseract OCR) and the report builders.
`deploy/start.sh` is its start command. Nothing in this folder runs on a workstation;
the workstation setup stays `scripts/bootstrap.ps1`.

## Before exposing it: reads are open

Every `GET` route of the server is unauthenticated by design (the app was built for one
machine). Project *changes* need the project password, but anyone who can reach the
address can read every project, every scraped page and every report, and download them.
Exposing the service beyond one machine therefore needs read access control in front of
it (the platform's private networking, an identity-aware proxy, a VPN, or basic auth on
a reverse proxy). Whether and how to add a login to the app itself is a product
decision; the server does not attempt it.

## Memory

Railway containers have 1 to 4 GB of memory and shared vCPUs. Measured on real sites:

| Work | Peak memory per batch |
| --- | --- |
| HTML-only scrape (browser renders, state exploration) | 0.8 to 1.2 GB |
| PDF-heavy scrape (documents and OCR as well) | 1.5 to 2.5 GB |
| Web app server, idle | about 250 MB plus the parsed runs it keeps |

Rules that keep a 2 GB container alive:

- `CP_DOC_PARALLEL_TASKS=1` (set in the image). One document is read at a time; 2 is
  acceptable at 4 GB. Never 4 (the workstation default) in a container.
- One scrape at a time. The shared queue already enforces this for the whole instance;
  do not run a second instance against the same data folder.
- Scrapes and project reports already run in their own processes, so the server's own
  memory does not grow with the largest site. The server keeps at most
  `CP_RUN_CACHE_ENTRIES` (default 4) fully parsed runs in memory; lower it to 2 at 1 GB.
- Choose the 4 GB size for PDF-heavy clients, or lower the batch size of their scrapes.

## Persistent volume

Mount one volume at `/data` (the image's `CP_DATA_ROOT`). `deploy/start.sh` places these
under it unless their own variable is set:

| Folder | Variable | What is lost without it |
| --- | --- | --- |
| `/data/app_data` | `CP_APP_DATA_ROOT` | projects, URL lists, scrapes, reports, the unlock-token key, the email outbox |
| `/data/evidence` | `CP_EVIDENCE_ROOT` | the captured pages every claim cites |
| `/data/url_history` | `CP_URL_HISTORY_ROOT` | the latest capture of every URL (reuse between scrapes) |
| `/data/knowledge_store` | `CP_KNOWLEDGE_ROOT` | the search index of each website (SQLite, when Postgres is not used) |
| `/data/model_cache` | `CP_MODEL_CACHE_DIR` | the downloaded CPU embedding model (only with the `fallback` extra) |
| `/data/spend` | `CP_SPEND_DIR` | paid-tier spend and approval logs (the caps rely on them) |
| `/data/exports` | `CP_EXPORTS_DIR` | the analyst API's exports folder (`--exports`) |

Railway: add a volume to the service and set its mount path to `/data`. The image runs as
root, so a freshly created volume is writable without further steps.

## Environment variables for a deployment

Set these on the service (see `.env.example` for every setting):

| Variable | Value | Why |
| --- | --- | --- |
| `PORT` | set by Railway | the server listens on it (`start.sh` passes it as `--port`) |
| `CP_BIND_HOST` | `0.0.0.0` (image default) | reachable from the platform's proxy |
| `CP_ALLOWED_HOSTS` | `myapp.up.railway.app` or `*.up.railway.app` | the server refuses every other `Host` header (DNS-rebinding guard); the platform's health check uses the same host name |
| `CP_ALLOWED_ORIGINS` | usually unset | hosts in `CP_ALLOWED_HOSTS` are accepted as origins; add a full origin only when the pages are served from another address |
| `CP_APP_BASE_URL` | `https://myapp.up.railway.app` | links in queue emails |
| `CP_BROWSER_CHANNEL` | `chromium` (image default) | no Google Chrome in the image; the bundled Chromium is used |
| `CP_DOC_PARALLEL_TASKS` | `1` (image default) | see Memory |
| `CP_DATABASE_URL` | optional Postgres DSN | the task ledger and, with pgvector, the knowledge base; without it the SQLite store under `/data` is used |
| `CP_SMTP_*`, `CP_SMTP_FROM` | optional | queue emails; otherwise they are written to `/data/app_data/outbox/` |
| `ENVIRONMENT` | `production` | refuses to boot with guardrails off |

## The browser in the image

Scrapes render pages with a real browser. On a workstation that is the installed Google
Chrome (`CP_BROWSER_CHANNEL=chrome`); when Chrome is absent the renderer falls back to
Playwright's bundled Chromium by itself (`src/integrations/browser_client.py`, `start()`).
The image sets `CP_BROWSER_CHANNEL=chromium` so the bundled browser is used straight away,
without a failed Chrome launch first. The Dockerfile installs that Chromium and its system
libraries (`playwright install --with-deps chromium`) into `/ms-playwright`, pinned to the
Playwright version in `pyproject.toml`'s `browser` extra. Chromium runs with its sandbox
off (Playwright's default), which is what lets it run as root in a container.

## Building and running elsewhere

```sh
docker build -t content-processor .
docker run --rm -p 8780:8780 -v cp-data:/data \
  -e CP_ALLOWED_HOSTS=localhost,127.0.0.1 -e CP_APP_BASE_URL=http://localhost:8780 \
  content-processor
```

Then open http://localhost:8780/. A smaller image leaves out the fonts for CJK scripts
(`fonts-noto-cjk`, about 60 MB) when no client site uses them.

## What the image does not include

- Ollama or any model server: point `CP_MODEL_ENDPOINT` at one on the private network, or
  build with `--build-arg EXTRAS=browser,docs,unblock,office,fallback` for the CPU
  embedding model (about 220 MB downloaded into `/data/model_cache` at first use).
- Tesseract language packs beyond `eng` and `osd`: add `tesseract-ocr-<lang>` packages to
  the `apt-get install` line for the client's languages.
- Bright Data credentials (paid tiers stay off until `BRIGHTDATA_ENABLED=true` and caps are set).
