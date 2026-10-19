# Content Processor — System Architecture (SDLC Step 2)

> **Document ID**: `CP-ARCH-2026-V1`
> **Status**: APPROVED for Stage 0 implementation (Step 3 sign-off given in chat by Gaurav Doshi on 2026-10-15: "go according to this layer approach"). Stage 0 scaffold lives under `src/modules/content_processor/`.
> **Date**: 2026-10-14
> **Inputs**: primary investigation (83 cases), unified investigation (90 cases), `Gap audit and extended edge-case inventory.md` (cases 91–214), Rankuno `project-standards` (StrictModel, BaseTool, RiskClass, CostLedger, inward-only dependency rule), `LOCAL_LLM_GATEWAY_STANDARD.md` (server resource hierarchy).
> **Target**: a run of 5,000 URLs minimum (design headroom to 50,000) completes with every URL carrying a terminal, evidence-backed status, surviving worker crashes, browser crashes, host blocks, poison inputs, and operator restarts, without disturbing the other services on the Rankuno server.

---

## 1. Scope, non-goals and provisional assumptions

**In scope (release 1)**: supplied URL lists; embedded assets of each URL; linked downloads to depth 1; HTML (static and rendered), PDF, raster images, SVG; declared viewport/locale/timezone; read-only, reversible interactions; owner-authorized and public-third-party hosts; honest per-URL status vector and gap list; streaming events; resumable runs.

**Flagged, not separate systems**: recursive site crawl (`mode=recursive_site`), Office and archive formats (Stage 3 adapters behind the same interface), audio/video (Stage 3), authenticated client areas (T0 only).

**Non-goals**: server-side source, CMS drafts, personalized experiences for every user, proving absence of undiscovered states, a single coverage number.

**Provisional assumptions** (from the decision register; each is a knob, not a constant): 5,000 URLs per run; mixed hosts; 85% HTML / 10% PDF / 5% other; avg 15 embedded assets per page; the first deployment runs inside WSL2 Docker on the shared Rankuno server at **P2 priority** (sacrificial, below Screaming Frog and the production Docker services); a dedicated crawl node follows in Stage 2.

---

## 2. Design axioms (binding)

1. **Denominator integrity**: failures, blocks and unsupported items stay in every denominator.
2. **Scope-bound claims**: the only positive terminal badge is `VERIFIED_WITHIN_SCOPE`, always displayed with its scope and capture context.
3. **Layer separation**: visible, collapsed-published, accessibility, hidden, metadata, source-only, uncertain, third-party-chrome, UGC, icon-artifact, paywalled-in-source, hidden-revision and design-evidence layers never merge silently.
4. **Provenance on every block**: method, version, selector/coordinates, state, evidence hashes, access tier.
5. **Terminal status on every entity** (resource, state, region, task).
6. **Immutable evidence first**: bytes and headers are stored before any transform; ledger rows reference evidence by hash.
7. **Reversible normalization**: original spans always retained.
8. **Crash-only workers**: any process may die at any instant; correctness depends only on the durable ledger.
9. **Politeness before throughput**: the host governor can starve the fleet; the fleet can never starve the governor.
10. **Security isolation**: workers hold no host credentials and can reach only public addresses through the egress proxy.
11. **Interaction restraint**: declared, reversible, GET-only actions; page content is data, never instruction.
12. **LLM annotates, never certifies**: model output carries evidence pointers and cannot change a status.

---

## 3. Topology

```
                         ┌─────────────────────────────────────────────┐
  operator / n8n ──────► │  API (FastAPI)  POST /runs  GET /runs/{id}   │
                         │  SSE /runs/{id}/events   GET /export         │
                         └──────────────┬──────────────────────────────┘
                                        │ writes RunScope, reads status views
                                        ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│  POSTGRES — single source of truth                                            │
│  runs · tasks(lease) · host_policy · resources · resource_edges · states ·    │
│  regions · extractions · evidence · gaps · events(append-only) · cost_ledger  │
└───────┬───────────────┬───────────────┬───────────────┬───────────────┬───────┘
        │ lease         │ lease         │ lease         │ lease         │ lease
        ▼               ▼               ▼               ▼               ▼
 ┌────────────┐  ┌────────────┐  ┌────────────┐  ┌────────────┐  ┌────────────┐
 │ scheduler  │  │ fetch-     │  │ browser-   │  │ doc-       │  │ verify-    │
 │ + reaper   │  │ worker ×N  │  │ worker ×M  │  │ worker ×K  │  │ worker ×V  │
 │ (1)        │  │ httpx /    │  │ Playwright │  │ PyMuPDF,   │  │ reconciler,│
 │ admission, │  │ curl_cffi  │  │ 1 browser, │  │ pypdf,     │  │ gap engine,│
 │ priorities,│  │ async      │  │ C contexts │  │ Tesseract, │  │ status     │
 │ host slots │  │            │  │            │  │ subprocess │  │ derivation │
 └─────┬──────┘  └─────┬──────┘  └─────┬──────┘  └─────┬──────┘  └─────┬──────┘
       │               │               │               │               │
       │               └───────┬───────┴───────┬───────┘               │
       │                       ▼               ▼                       │
       │              ┌────────────────┐ ┌──────────────┐              │
       │              │ EGRESS PROXY   │ │ EVIDENCE     │◄─────────────┘
       │              │ (public-only   │ │ STORE        │
       │              │  allowlist,    │ │ MinIO/S3     │
       │              │  per-tier      │ │ content-     │
       │              │  upstreams)    │ │ addressed    │
       │              └───────┬────────┘ └──────────────┘
       │                      ▼
       │               internet / proxies (T4) / unblockers (T5)
       │
       └──► annotate-worker (optional, FINANCIAL) ──► LLM Gateway (Ollama → OpenRouter)
            media-worker (Stage 3, optional)        ──► ffmpeg / whisper
```

Every arrow into Postgres is a lease, heartbeat, checkpoint or append. No worker talks to another worker. Workers are horizontally scalable by process count and are safe to kill with `SIGKILL`.

### Component responsibilities

| Component | Responsibility | Must never |
|---|---|---|
| API | Validate `RunScope`, create run, expose status vector, events, export, cancel/resume | Execute work in-process |
| Scheduler/reaper | Admit URLs into the frontier by priority and backpressure; expand discovered resources into tasks; reclaim expired leases; quarantine poison tasks; derive run status | Fetch anything |
| Host governor (library used by scheduler and workers; state in `host_policy`) | Per-host concurrency slots, delay, robots cache, circuit breaker, block score, access tier | Be process-local |
| fetch-worker | T1/T2 HTTP acquisition, MIME sniff, integrity checks, raw evidence write, block classification | Render, parse documents |
| browser-worker | T3 rendering, state exploration, multi-channel capture (source, DOM, ARIA, screenshots, network bodies), block classification | Parse PDFs, run OCR |
| doc-worker | PDF/image/Office/archive adapters in a resource-limited subprocess; region inventory; region OCR | Make network requests |
| verify-worker | Reconcile channels, map extractions to regions, emit gaps, derive statuses, request remediation tasks | Invent content |
| annotate-worker | LLM section labels, inconsistency flags, retry prioritization with evidence pointers | Change any status |
| export | JSONL/Parquet export, Postgres views, n8n webhook on terminal states | Drop gaps |

---

## 4. Run lifecycle

```
ADMITTED ─► FRONTIER_BUILT ─► ACQUIRING ─► RENDERING/EXTRACTING ─► VERIFYING ─► REMEDIATING ─► EXPORTED
    │                                                                                    │
    └──────────────── CANCELLED / BUDGET_STOPPED / RESUMED (idempotent) ─────────────────┘
```

1. **Admission**: `RunScope` validated (StrictModel), `scope_hash` computed, authorization checked against the host registry (legal holds), tool versions pinned into the run row, `CostLedger` ceiling set.
2. **Frontier build**: input URLs normalized to `schedule_key` (case 91), aliases recorded, invalid inputs excluded (92), SSRF classification (93), mandatory URLs flagged (100), per-host counts computed. One `FETCH` task per schedule key, priority by class.
3. **Acquisition**: fetch-worker leases a task if the host governor grants a slot. Raw bytes → evidence store → `resources` row → block classifier → next task (`RENDER` for HTML, `EXTRACT_DOC` for documents, `EXTRACT_IMAGE` for images) or terminal status.
4. **Rendering and state exploration**: browser-worker captures the base state, inventories regions, controls, frames, scroll surfaces and assets; explores allowed actions within budgets; persists each state's channels; emits discovered resources as new tasks (edges recorded).
5. **Document extraction**: doc-worker inventories pages/parts/objects, runs native extraction, detects text-bearing visual regions, OCRs regions (never pages when a native layer exists; never suppressed by a native header), reconciles native vs OCR.
6. **Verification**: verify-worker runs the deterministic rule set (§10), produces `gaps`, derives entity statuses and the URL badge, requests remediation tasks (re-render with longer budget, OCR at higher DPI, alternate parser, tier escalation) while the run budget allows.
7. **Remediation**: bounded loop; every remediation is a new attempt row, never an overwrite.
8. **Export**: per-URL records, blocks, gaps, evidence index; n8n webhook; status views.

Events are appended at every transition (§11). Cancel sets `runs.cancel_requested`; workers check it on heartbeat and release leases. Resume re-leases only `PENDING`/`LEASED-expired` tasks; completed work is never repeated.

---

## 5. Durable work model (why it cannot lose or duplicate work)

Postgres table `tasks` (ADR-0001):

```
id · run_id · kind · idempotency_key (unique) · host · priority · state
attempts · max_attempts · not_before · lease_owner · lease_expires_at · heartbeat_at
payload jsonb · last_error · created_at · finished_at
```

- **Lease**: `UPDATE tasks SET state='LEASED', lease_owner=$w, lease_expires_at=now()+$ttl WHERE id = (SELECT id FROM tasks WHERE state='PENDING' AND not_before<=now() AND kind=ANY($kinds) AND host_slot_available(host) ORDER BY priority, id FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *`.
- **Heartbeat**: every 15 s the worker extends `lease_expires_at`; a long render extends the lease, a dead worker does not.
- **Checkpoint**: large tasks (state exploration, multi-page OCR) write progress into `payload.checkpoint` so a reclaimed task resumes at the last state/page instead of restarting.
- **Reaper**: every 30 s, `LEASED` tasks past expiry return to `PENDING` with `attempts+1` and a `task_reclaimed` event. At `attempts >= max_attempts` the task becomes `QUARANTINED`, the owning entity gets `FAILED(poison)` and a gap with the last error and any partial evidence.
- **Idempotency**: `idempotency_key = sha256(run_id, kind, schedule_key|resource_hash, state_key, page_or_region_key)`. Re-delivery upserts; region and extraction rows are keyed the same way, so a task that ran twice cannot double-count.
- **Evidence-first ordering**: workers write bytes to the evidence store, then insert the ledger row referencing the hash, then mark the task done, in that order. A crash between steps leaves an orphaned object (swept weekly) or a `PENDING` task, never a ledger row pointing at missing evidence.
- **Retry-After and backoff**: `not_before` carries the earliest retry time; the governor also enforces host-level `next_allowed_at`.
- **Priority classes**: `0 mandatory URLs`, `1 HTML pages`, `2 documents`, `3 embedded assets`, `4 OCR/remediation`, `5 annotation`. Within a class, FIFO by id.

Throughput for Postgres at this scale: a 5,000-URL run creates roughly 110k tasks and 1–2M events; comfortably within a single Postgres instance with the indexes `(state, kind, priority, id)` and `(run_id, host)`.

---

## 6. Worker fleet

| Worker | Runtime | Concurrency per process | Memory ceiling (cgroup) | Recycle policy | Timeouts |
|---|---|---|---|---|---|
| fetch-worker | Python 3.11, asyncio, `httpx` (T1) / `curl_cffi` (T2) | 32 in-flight, governor-bound per host | 512 MB | restart every 10k tasks | connect 10 s, read 30 s, total 120 s, body cap per scope |
| browser-worker | Playwright, real Chrome channel, 1 browser per process | C = 3 contexts (shared server) / 6 (dedicated node) | 3 GB | browser restart every 50 pages or when RSS > 2.2 GB or after 30 min; context per task | navigation 30 s, settle window 10 s, per-URL action budget, hard page cap 180 s (scope-configurable) |
| doc-worker | Python; each document in a child process with `RLIMIT_AS`, `RLIMIT_CPU`, no network | 2 documents | 1.5 GB parent + 1 GB per child | child per document | per-document wall clock, per-page OCR time, pixel/page caps |
| ocr pool (inside doc-worker) | Tesseract 5 via `pytesseract`, `psm` per region, language packs per scope | CPU threads − 2 | included above | n/a | per-region time cap |
| verify-worker | Python, DB-bound | 8 | 512 MB | n/a | per-URL verification cap |
| annotate-worker | Python → LLM Gateway | 2 | 256 MB | n/a | gateway timeouts; `FINANCIAL` |
| media-worker (Stage 3) | ffmpeg, whisper via gateway or local | 1 | 2 GB | per file | per-minute-of-media caps |

Extraction routes and the "second parser only on trigger" rule (ADR-0003):

| Route | Primary | Second opinion trigger | Second tool | Visual detector |
|---|---|---|---|---|
| HTML source | `selectolax` (fast) | parse warnings, malformed markup | `lxml` recover mode / `BeautifulSoup(html5lib)` | n/a |
| Rendered DOM | Playwright composed-tree walker | always captured alongside source | n/a | screenshot + text-region detector |
| Structured data | `extruct` | n/a | manual JSON-LD parse on failure | n/a |
| PDF native | PyMuPDF spans with coordinates | garbled ratio > 2%, empty page with image objects, order ambiguity, disagreement with render | `pdfplumber` (layout) and `pypdf` (objects, forms, annotations, attachments) | render at 150 dpi + text-region detector; region OCR |
| PDF tables | `pdfplumber` | header/continuation ambiguity | `camelot` lattice | n/a |
| Image | Pillow + Tesseract region OCR | confidence < threshold, rotated/multilingual | cloud OCR (FINANCIAL, optional) | text-region detector |
| Office (Stage 3) | Docling | unsupported part or disagreement | Tika (forked JVM, isolated) | render via LibreOffice headless + OCR |

Every route returns the same `ExtractionResult` contract; the adapter registry maps sniffed MIME → route, and anything unmapped is `UNSUPPORTED` with the sniff evidence.

### 6.1 OCR language routing (decided 2026-10-14: multilingual from Stage 1, local engines only)

Routing happens **per region**, not per document, because a single page can mix scripts (an English letterhead over a Hindi body, a Gujarati price list with Latin SKUs).

```
region crop ─► script detection ─► language pack(s) ─► Tesseract (fast) ─► confidence gate
                   │                                            │
                   │                                            └─ low confidence → trigger: Tesseract (best) or PaddleOCR
                   └─ no installed pack for script → UNSUPPORTED(ocr_lang:<script>) gap, crop kept as evidence
```

| Step | Mechanism | Notes |
|---|---|---|
| Script hint from context | `lang` attributes, `Content-Language`, PDF font names and `ToUnicode` ranges, page-level native text script, scope locale | Free signals; used to order candidates, never to skip detection |
| Script detection | Tesseract OSD (`osd.traineddata`) on the crop for script and orientation; Unicode-range vote on any native text near the region | Rotation handled here (case 154) |
| Pack selection | `script → packs` table, e.g. Devanagari → `hin+mar+san+nep` narrowed by context hint; Latin → `eng` plus scope locale; Arabic → `ara+fas+urd`; CJK → `chi_sim`/`chi_tra`/`jpn`/`kor` by hint; Bengali → `ben`; Gurmukhi → `pan`; Gujarati → `guj`; Tamil → `tam`; Telugu → `tel`; Kannada → `kan`; Malayalam → `mal`; Odia → `ori`; Cyrillic → `rus+ukr`; Greek → `ell`; Hebrew → `heb`; Thai → `tha` | Multi-pack `-l a+b` costs ~linear time; cap at 3 packs per region |
| First pass | `tessdata_fast` models, `psm` chosen by region shape (6 block, 7 line, 11 sparse) | Fast models keep the shared server inside its CPU budget |
| Confidence gate | mean word confidence and critical-token checks (R9) | Threshold set from Stage 0 fixtures, not guessed |
| Second opinion (trigger only, ADR-0003) | `tessdata_best` for the same packs; PaddleOCR (local, free) evaluated in Stage 1 for Indic and CJK where Tesseract is weak | Both readings retained as alternatives |
| Bidi and shaping | ICU bidi reordering for Arabic/Hebrew; Indic conjunct normalization kept reversible | Original glyph order preserved in `raw_text` |

Image packaging: a **core** image ships `eng, hin, mar, guj, ben, tam, tel, kan, mal, pan, ori, urd, ara, chi_sim, chi_tra, jpn, kor, rus, deu, fra, spa, por, ita, nld, osd` in both fast and best variants (≈ 400 MB); an **extended** image adds the rest of the Tesseract pack set. A scope may pin `ocr_languages` to shrink the candidate set; it cannot widen beyond installed packs. Every OCR extraction records `engine, model_variant, packs, psm, dpi, preprocessing` so reviewers can rerun it.

Cost: $0. Cloud OCR remains a registered FINANCIAL route but is unreachable while `cost_ceiling_usd = 0`.

---

## 7. Host governor (the component that keeps us unblocked and keeps client sites up)

State lives in `host_policy` (one row per `(tenant, host)`), updated transactionally; every worker reads it before a request and writes outcome counters after.

```
host · tenant · max_concurrency · in_flight · base_delay_ms · next_allowed_at
robots_cache · robots_fetched_at · crawl_delay · circuit_state · circuit_opened_at
consecutive_failures · clean_streak · block_score · access_tier · tier_ceiling
session_profile_id · egress_ip_class · last_429_at · retry_after_until · legal_hold
baseline_main_words · challenge_rate_50 · notes
```

Rules (defaults; all scope-overridable within the ceiling):

- Concurrency 1 per host; 2 after 50 clean responses on hosts behind a CDN; never above 4 unless `owner_authorized`.
- Delay = max(crawl-delay, 2 × p50 latency, 500 ms) with ±30% jitter; 429 → delay ×2, concurrency ÷2, `retry_after_until` honoured; 5xx → bounded exponential backoff with jitter.
- Circuit breaker: 5 consecutive non-transient failures → `OPEN` (15 min) → `HALF_OPEN` (one probe) → `CLOSED`.
- Block classifier verdicts update `block_score`; escalation per the playbook state machine; tier changes create a new session profile; decay one tier per 200 clean responses.
- Global caps: per-ASN requests/min across all tenants; per-tenant proxy GB per run.
- Slots are granted by a single `UPDATE … WHERE in_flight < max_concurrency AND next_allowed_at <= now() AND circuit_state <> 'OPEN' RETURNING` statement, so two workers cannot both take the last slot.
- Browser subrequests count toward the host's slot (one render = one slot for its duration), and toward third-party hosts' budgets only as asset fetches (not renders).

---

## 8. Backpressure and admission

Each task kind has a depth watermark. The scheduler admits new `FETCH` tasks only while `pending(RENDER) < 200`, `pending(EXTRACT_DOC) < 100`, `pending(OCR) < 300`, and the evidence budget for the run is under 80%. Workers never pull more than their concurrency. Document and OCR concurrency is fixed, so 5,000 PDFs arriving at once simply wait in Postgres, costing nothing. A run therefore degrades to "slower" and never to "out of memory".

Run-level budgets (scope): max wall clock, max requests, max bytes, max evidence bytes, max actions per URL, max remediation rounds, `CostLedger` USD ceiling. Any budget exhaustion ends the run as `PARTIAL(budget_stopped)` with the frontier preserved for resume.

---

## 9. Data contracts (Pydantic v2, `StrictModel`)

All models inherit `src.core.schemas.StrictModel` (`extra="forbid"`, `validate_assignment=True`). Enums are `StrEnum`. Confidence fields are bounded `[0, 1]`. Shown here abbreviated; the full module is `src/modules/content_processor/contracts.py`.

```python
class ScopeMode(StrEnum):
    SUPPLIED_URLS = "SUPPLIED_URLS"
    SUPPLIED_URLS_PLUS_EMBEDDED = "SUPPLIED_URLS_PLUS_EMBEDDED"
    RECURSIVE_SITE = "RECURSIVE_SITE"


class AuthorizationKind(StrEnum):
    OWNER_AUTHORIZED = "OWNER_AUTHORIZED"
    PUBLIC_THIRD_PARTY = "PUBLIC_THIRD_PARTY"
    LICENSED_SOURCE = "LICENSED_SOURCE"


class AccessTier(StrEnum):
    T0_AUTHORIZED = "T0_AUTHORIZED"
    T1_POLITE_HTTP = "T1_POLITE_HTTP"
    T2_IMPERSONATED_HTTP = "T2_IMPERSONATED_HTTP"
    T3_FIDELITY_BROWSER = "T3_FIDELITY_BROWSER"
    T4_PROXIED = "T4_PROXIED"
    T5_MANAGED_UNBLOCKER = "T5_MANAGED_UNBLOCKER"
    T6_POLICY_GATED = "T6_POLICY_GATED"


class Authorization(StrictModel):
    kind: AuthorizationKind
    reference: str = Field(min_length=1, description="Contract/ticket id or 'public'")
    approved_by: str
    approved_at: datetime
    hosts: list[str] = Field(min_length=1)


class InteractionPolicy(StrictModel):
    allowed: list[
        Literal[
            "scroll",
            "expand_disclosure",
            "select_tab",
            "hover_info",
            "open_informational_dialog",
            "keyboard_traverse",
            "paginate_get",
        ]
    ]
    max_actions_per_url: int = Field(default=50, ge=0, le=500)
    max_scroll_passes: int = Field(default=20, ge=0, le=200)
    side_effect_rule: Literal["get_only_no_navigation"] = "get_only_no_navigation"


class Budgets(StrictModel):
    max_runtime_s: int = Field(ge=60, le=7 * 24 * 3600)
    max_requests: int = Field(ge=1)
    max_bytes: int = Field(ge=1)
    max_evidence_bytes: int = Field(ge=1)
    max_remediation_rounds: int = Field(default=2, ge=0, le=5)
    cost_ceiling_usd: float = Field(default=0.0, ge=0.0)


class RunScope(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    tenant_id: str
    mode: ScopeMode
    input_urls: list[str] = Field(min_length=1, max_length=100_000)
    mandatory_urls: list[str] = []
    hosts_in_scope: list[str] = Field(min_length=1)
    linked_download_depth: int = Field(default=1, ge=0, le=3)
    allowed_mime_families: list[str]
    interaction_policy: InteractionPolicy
    viewport: tuple[int, int] = (1440, 900)
    locale: str = "en-US"
    timezone: str = "UTC"
    geography: str = "US"
    consent_state: Literal["ordinary_public_choices_applied", "none"]
    access_context: Literal["public_anonymous", "authenticated_client"]
    authorization: Authorization
    access_tier_ceiling: AccessTier
    robots_policy: Literal["respect", "respect_with_exceptions", "informational"]
    analytics_suppression: bool = False
    content_includes: list[str]
    content_excludes: list[str]
    budgets: Budgets
    retention_class: Literal["standard_90d", "extended_1y", "legal_hold"]
    pii_policy: Literal["tag_and_redact_exports", "tag_only"]
    recrawl_of_run_id: str | None = None
```

Ledger entities (`resources`, `resource_edges`, `states`, `regions`, `extractions`, `evidence`, `gaps`, `events`) are each a `StrictModel` mirrored by a Postgres table. Key contracts:

```python
class Resource(StrictModel):
    resource_id: str
    run_id: str
    requested_url: str
    final_url: str
    schedule_key: str
    aliases: list[str]
    redirect_chain: list[RedirectHop]  # typed hops: http|meta|js|canonical_hint
    declared_mime: str | None
    sniffed_mime: str
    extension_hint: str | None
    content_disposition_name: str | None
    http_status: int | None
    transport_error: str | None
    payload_sha256: str | None
    payload_bytes: int | None
    acquired_at: datetime | None
    acquired_at_tier: AccessTier | None
    block_verdict: BlockVerdict | None
    scope_decision: Literal["in_scope", "excluded_by_policy", "out_of_scope_asset"]
    mandatory: bool
    status: EntityStatus
    status_reason: str | None
    attempt: int = Field(ge=1)


class ContentBlock(StrictModel):
    block_id: str
    block_key: str  # stable across recrawls (case 210)
    resource_id: str
    state_id: str
    region_id: str
    frame_path: str
    heading_path: list[str]
    reading_order: int
    page_index: int | None
    page_label: str | None
    bbox: tuple[float, float, float, float] | None
    layer: ContentLayer
    role: Literal["main", "nav", "footer", "aside", "legal", "chrome", "unknown"]
    language: str | None
    raw_text: str
    normalized_text: str
    normalization_ops: list[str]  # reversible
    method: str
    tool_version: str
    confidence: float = Field(ge=0.0, le=1.0)
    alternatives: list[AlternativeReading] = []
    status: EntityStatus
    evidence_ids: list[str] = Field(min_length=1)
    provenance: Provenance  # selector, xpath, pdf object id, OCR params
    claim_context: ClaimContext | None  # entity, variant, dates, audience (case 212)
    dedupe_of: str | None


class Gap(StrictModel):
    gap_id: str
    run_id: str
    resource_id: str
    state_id: str | None
    region_id: str | None
    gap_type: GapType
    status: EntityStatus
    reason: str
    evidence_ids: list[str]
    retry_eligible: bool
    tier_reached: AccessTier | None
    inventory_revision: int


class UrlRecord(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    run_id: str
    requested_url: str
    final_url: str
    aliases: list[str]
    captured_at: datetime
    tool_versions: dict[str, str]
    badge: UrlBadge  # RUNNING…EXCLUDED
    status_vector: StatusVector  # discovery, acquisition, fidelity, state exploration
    inventory: InventoryCounts  # denominators explicit, revision number
    discovery: DiscoveryState
    gaps: list[Gap]
    blocks: list[ContentBlock]
    template_fingerprint: str | None  # case 209
    design_evidence_id: str | None  # case 214
    warnings: list[str]
```

`ToolMetadata` declarations (Rankuno Step 2 checklist):

| Tool | Risk class | `rate_limit_key` | Cost |
|---|---|---|---|
| `content.run_extraction` | READ | none (governor-bound) | 0 |
| `content.fetch_http` | READ | `host:<host>` | 0 |
| `content.render_browser` | READ | `host:<host>` | 0 |
| `content.extract_document` | READ | none | 0 |
| `content.ocr_cloud` | FINANCIAL | `vendor:<name>` | per page |
| `content.proxy_egress` | FINANCIAL | `proxy:<vendor>` | per GB |
| `content.unblocker` | FINANCIAL | `unblocker:<vendor>` | per request |
| `content.annotate_llm` | FINANCIAL | `llm:gateway` | per 1k tokens |
| `content.transcribe` | FINANCIAL | `asr:<vendor>` | per minute |

---

## 10. Verification engine ("the brain")

Deterministic rules run per URL after every state or extraction change; each emits gaps with evidence pointers.

| Rule | Check | Gap emitted |
|---|---|---|
| R1 inventory accounting | every discovered resource/state/region has a terminal status | `DISCOVERY_OPEN` until true |
| R2 acquisition | required resources acquired vs discovered; blocked and failed remain | `ACCESS_BLOCKED` / `ACQUISITION_FAILED` |
| R3 frame outcomes | every frame in the tree has load + extraction outcome | per-frame gap |
| R4 control coverage | every enumerated disclosure/tab/pager visited or listed | `UNVISITED_STATE` |
| R5 virtualization reconcile | unique observed items vs declared total (`aria-rowcount`, "showing N of M") | `PARTIAL` with ranges |
| R6 visual text regions | text-region detector regions on screenshots/renders map to an extraction or an explicit reason | `LOW_CONFIDENCE` / `FIDELITY_DISAGREEMENT` |
| R7 channel agreement | source text vs DOM vs ARIA vs OCR on sampled regions; obfuscated-font detection (case 114) | `FIDELITY_DISAGREEMENT` |
| R8 PDF regions | image objects on pages with native text got region OCR; native vs OCR comparison | per-region gap |
| R9 critical tokens | numbers, units, currencies, negations, footnote markers preserved from raw to normalized | `FIDELITY_DISAGREEMENT` |
| R10 payload sanity | not a challenge page, not a soft 404, not a soft redirect, MIME agrees with sniff, not truncated | `ACCESS_BLOCKED` / `SOFT_REDIRECT_SUSPECTED` |
| R11 degradation | main-region words vs host baseline; canary disagreement | `SHADOW_BLOCK_SUSPECTED` / `DEGRADED_VARIANT` |
| R12 budgets | any limit hit | `LIMIT_HIT` / `BUDGET_STOPPED` |
| R13 layer hygiene | no source-only, hidden, icon-artifact, or third-party-chrome text in the main corpus | corpus rejection with pointer |

Status derivation: entity statuses from rules; URL badge by precedence over the vector; run badge by precedence over URLs. A badge never erases the vector.

Visual text-region detector: Tesseract `psm 11` sparse-text mode at 150 dpi as the baseline detector, with a pluggable interface for a dedicated detector (e.g. PaddleOCR det) if Stage 1 fixtures show misses. The detector's own misses are an accepted, documented limitation.

LLM annotator (via the LLM Gateway, Ollama first, OpenRouter fallback): section labelling, inconsistency flags, remediation prioritization. Inputs are block text plus evidence ids; outputs are stored as annotations with model/version and never feed R1–R13. **With `cost_ceiling_usd = 0` (decided 2026-10-14) the annotator runs only when the gateway reports a local Ollama provider; the OpenRouter fallback is refused by the cost ledger, and the run records `annotator: disabled(cost_ceiling)`.** Nothing in the status vector depends on it.

---

## 11. Real-time status

`events` is append-only: `seq · run_id · entity_type · entity_id · old_state · new_state · inventory_revision · reason · attempt · evidence_ids · at`. Each insert does `pg_notify('run_events', run_id)`. The API exposes `GET /runs/{id}/events` as Server-Sent Events with `Last-Event-ID` resume, and `GET /runs/{id}` returns the live vector:

```
URL https://example.org/product · badge PARTIAL · inventory rev 12
resources 7 discovered / 6 acquired / 1 blocked
regions 24 observed / 18 verified / 3 review / 1 blocked / 2 unvisited
discovery OPEN (2 controls unvisited; action budget exhausted)
tier reached T3 · cost $0.00 · elapsed 41 s
```

Three latencies are recorded per URL: first evidence, first gap event, terminal badge. n8n receives a webhook on run terminal state and optionally on every `BLOCKED`.

---

## 12. Security isolation

- **Egress proxy** (Squid or Envoy) is the only network path for fetch-, browser- and media-workers. It resolves DNS itself, refuses private, loopback, link-local, multicast, and cloud-metadata ranges, refuses non-HTTP(S) schemes, and re-checks every redirect hop. DNS rebinding is defeated because the proxy connects to the address it resolved. T4/T5 upstreams are configured on the proxy per tier, never in worker code.
- **Workers** run as non-root in containers with read-only root FS, `tmpfs` scratch with size limits, `no-new-privileges`, seccomp default profile, cgroup memory/CPU limits, no host credentials, no Docker socket. doc-worker children have no network namespace.
- **Parsers**: macros disabled; PDF JavaScript/Launch actions never executed; archive extraction validates member paths, depth, count and expansion ratio; image decoders bounded by pixel count; Tika (if used) in a forked JVM with heap limit and timeout.
- **Secrets**: `get_settings()` with `SecretStr` only in the API/scheduler; workers receive per-task short-lived credentials (presigned evidence URLs, proxy session tokens) in the lease payload.
- **Tenant isolation**: evidence bucket prefix per tenant, proxy sub-pool per tenant, storage-state credentials encrypted with a per-tenant key, retention and deletion jobs per tenant.
- **Content as data**: no extracted string is ever used to build a command, a selector, or a prompt instruction without escaping; LLM prompts wrap page text as quoted data.
- **Evidence access**: raw bytes restricted; sanitized crops and hashes available to reviewers; signed-URL tokens redacted in all user-facing artefacts.

---

## 13. Observability and SLOs

Structured JSON logs with `trace_id` (Rankuno logger) per task; metrics via Prometheus exposition: queue depths by kind, lease age, reclaim rate, quarantine count, per-host counters (§7), per-tier success rate, OCR seconds per page, evidence bytes per URL, cost per run, three latencies. Dashboards per run and per host. Alerts: reclaim rate > 5% over 10 min; any worker RSS above 90% of ceiling; challenge rate > 10% on a host; cost at 80% of ceiling; evidence at 80% of budget; run wall clock at 80%.

Provisional SLOs to be validated in Stage 1 (not promises): time-to-first-evidence p50 < 5 s per URL; time-to-first-gap-event p50 < 30 s; crash recovery (reaper reclaim) < 2 min; zero lost tasks across a forced worker kill test; zero duplicate blocks across a forced double-delivery test.

---

## 14. Capacity plan for 5,000 URLs

All numbers are planning assumptions to be measured in Stage 0/1.

| Parameter | Shared Rankuno server (WSL2 Docker, P2) | Dedicated crawl node (8 vCPU / 32 GB) |
|---|---|---|
| browser contexts | 3 | 12 (2 workers × 6) |
| doc-worker documents in parallel | 2 | 4 |
| OCR threads | 4 | 8 |
| fetch in-flight (global) | 32 | 128 |
| avg render + explore per HTML URL | 20 s | 20 s |
| HTML throughput (render-bound) | ~9 URL/min | ~36 URL/min |
| 4,250 HTML URLs render time | ~8 h | ~2 h |
| 500 PDFs (avg 12 pages, 30% needing region OCR) | ~2 h (parallel with render) | ~45 min |
| politeness bound if all 5,000 URLs are on one host at 1 req/2 s + 15 assets/page (assets deduped ~60%) | ~9–10 h | same bound (host-limited) |
| **Expected wall clock** | **9–12 h** | **2–4 h** (multi-host) / host-bound (single host) |
| Evidence at default policy (raw bytes, 1 JPEG full-page per state, crops for gaps) | ~10–12 GB | same |
| Postgres rows | ~110k tasks, ~1.5M events, ~400k regions | same |

Shared-server memory budget inside the WSL2 VM (currently ~15 GB): MinIO 0.5 GB, API+scheduler 0.5 GB, fetch-worker 0.5 GB, browser-worker 3 GB cap, doc-worker 2.5 GB cap, verify 0.5 GB ≈ **7.5 GB**, leaving the existing n8n/Postgres/Qdrant/Outline Generator untouched. Raising the WSL2 memory cap to 20 GB is recommended before Stage 2; the crawler remains **P2** and is the first thing throttled (contexts 3 → 1) when host pressure appears. Screaming Frog is never in the control loop.

Knobs that change the estimate most: per-host concurrency (politeness), action budget per URL, OCR DPI, screenshot policy, number of states explored.

---

## 15. Failure-mode catalogue (the "does not crash" argument)

| Failure | Detection | Consequence | Recovery |
|---|---|---|---|
| Worker process killed (`SIGKILL`, OOM) | lease expiry | task reclaimed with `attempts+1`; partial evidence retained | reaper, ≤ 2 min |
| Browser crash / renderer OOM | Playwright exception, RSS watchdog | context lost; task checkpoint resumes at last state | browser restart; attempt counter |
| Poison URL (crashes 3×) | `attempts >= max_attempts` | `FAILED(poison)` with last error and evidence | quarantine; operator can re-queue after fix |
| Postgres restart | connection errors | workers block/retry with backoff; no work lost (leases are rows) | reconnect; reaper handles stale leases |
| MinIO unavailable | upload failure before ledger write | task retried; nothing recorded as done | backoff; alert |
| Egress proxy down | all fetches fail transport | host circuits open; run pauses | alert; resume on recovery |
| Host blocks mid-run | classifier verdict | host escalates within ceiling or `BLOCKED` per resource | state machine §7 |
| Disk full (evidence) | quota counter | run `PARTIAL(limit_hit:evidence)`; frontier preserved | raise quota, resume |
| Run exceeds wall clock | budget | `PARTIAL(budget_stopped)` with frontier | resume with new budget |
| Deploy of new extractor version mid-run | version pin mismatch | new workers refuse tasks from runs pinned to old version | drain old workers; new run for new version |
| Duplicate task delivery | idempotency key | upsert; no double count | none needed |
| Operator cancel | `cancel_requested` | workers release leases at next heartbeat | n/a |
| Operator restart of everything | all leases expire | run resumes exactly where it was | reaper |
| Clock skew | NTP check at boot | refuse to start if skew > 5 s | fix NTP |

---

## 16. Integration with existing Rankuno infrastructure

| Existing | Use |
|---|---|
| PostgreSQL (WSL2 Docker) | ledger, task queue, host governor, events; separate database `content_processor` |
| Qdrant | Stage 4 only (block embeddings keyed by `block_id`, filtered by status and layer) |
| n8n | run triggers (`POST /runs`), terminal-state webhooks, review-queue routing |
| LLM Gateway (Ollama → OpenRouter) | annotate-worker; provider decided at runtime per the gateway standard |
| Screaming Frog | out of the loop; its crawl exports can seed `input_urls` and sitemaps; it remains P0 |
| `project-standards` core | `StrictModel`, `BaseTool`, `ToolMetadata`, `CostLedger`, `TokenBucket`, logger, guardrails |
| New | MinIO (evidence), egress proxy container, Prometheus/Grafana (or the existing monitoring if present) |

---

## 17. Repository layout (inward-only dependency rule)

```
content-processor/
├── src/
│   ├── core/                     # copied/vendored from project-standards (ADR per Rankuno precedent)
│   ├── integrations/
│   │   ├── http_client.py        # httpx + curl_cffi behind one interface (T1/T2)
│   │   ├── browser_client.py     # Playwright wrapper, stealth fork behind interface (T3)
│   │   ├── proxy_provider.py     # T4 vendors
│   │   ├── unblocker_provider.py # T5 vendors
│   │   ├── evidence_store.py     # MinIO/S3 content-addressed
│   │   ├── llm_gateway.py
│   │   └── ocr_cloud.py          # optional FINANCIAL
│   └── modules/content_processor/
│       ├── contracts.py          # all StrictModels (§9)
│       ├── scope/                # normalization, SSRF classification, authorization checks
│       ├── ledger/               # repositories, task leasing, reaper, events
│       ├── governor/             # host policy, circuit breaker, block classifier, escalation
│       ├── workers/              # fetch, browser, doc, verify, annotate, media
│       ├── adapters/             # html, pdf, image, office, archive, media (registry)
│       ├── verify/               # rules R1–R13, status derivation, gap engine
│       ├── api/                  # FastAPI app, SSE
│       └── export/
├── fixtures/                     # Stage 0 adversarial suite (served by a local fixture server)
├── tests/                        # mirrors src/; chaos tests for §15
├── docs/                         # this file, playbook, adr/
└── reports/                      # investigations
```

`modules → integrations → core` only. Anything that violates the rule fails CI.

---

## 18. Staged build plan with exit criteria

| Stage | Deliverables | Exit criteria (all testable) |
|---|---|---|
| **0 — Contract and fixtures** (2–3 weeks) | `contracts.py`; Postgres DDL; task ledger + reaper; host governor skeleton; local fixture server hosting ≥ 60 fixtures covering the true-positive and false-positive traps from the investigations plus cases 91, 103–106, 113, 114, 118, 122, 123, 177, 178, 191; chaos test harness (kill worker, double-deliver, Postgres restart) | every fixture has an expected `UrlRecord` with expected gaps; chaos tests pass with zero lost or duplicated tasks; `scripts/verify.ps1` green; Step 3 sign-off on contracts |
| **1 — Audited capture MVP** (4–6 weeks) | fetch-worker (T1/T2), browser-worker (T3, base state only), PDF/image doc-worker with region OCR, evidence store, verify rules R1–R3, R6, R8–R10, R12–R13, SSE events, export, egress proxy | every supplied URL has a terminal badge; every block has provenance and evidence; zero false `VERIFIED_WITHIN_SCOPE` on the adversarial suite; a 5,000-URL mixed-host dry run on the shared server completes within the planned window with no manual intervention |
| **2 — Dynamic coverage and resilience** (4–6 weeks) | state explorer (tabs, disclosures, nested scroll, 2-axis virtualization, frames, hover/keyboard), block classifier + escalation to the ceiling, proxy provider (T4), remediation loop, template fingerprint, dedicated crawl node | required state/resource counts reconcile on fixtures; budget stops remain `PARTIAL`; per-host block rate below the Stage 1 baseline on the sampled public set; cost ledger enforced |
| **3 — Format breadth** (4–8 weeks) | Office, archive, email, legacy via sandboxed conversion; media worker; multilingual OCR routing; cloud OCR fallback (FINANCIAL) | per-feature fixtures pass; unsupported internals remain visible; cost per 1,000 documents measured |
| **4 — Website analysis** (separate plan) | main-content views, boilerplate labels, topic themes, exact and semantic similarity, contradiction candidates with claim context, Qdrant | every result traces to evidence and carries gap-aware confidence; contradiction candidates require matched context |

Stage 0's release gate is the corpus gate: **zero false `VERIFIED_WITHIN_SCOPE` on the known-missing adversarial suite**, plus the chaos gate: **zero lost or duplicated work under forced failures**.

---

## 19. Decision register (carried from the gap audit, Part E)

Scope mode; authorized vs third-party hosts; UGC; soft paywalls; retention; deployment target; wall-clock target; theming meaning; acceptable review volume. Defaults are stated in the gap audit and encoded as scope defaults, not hidden in code.

**Decided 2026-10-14** (encoded as scope defaults in `contracts.py`):

| Decision | Value | Effect in code |
|---|---|---|
| Cost ceiling and paid tiers (final, 2026-10-15) | `cost_ceiling_usd = 0.0` and `access_tier_ceiling = T3` by default in every profile; paid tiers only on operator approval, per host, with a small cap (default $5 per run; $25/day and operator-set monthly caps) funded by the Bright Data subscription | The governor returns `NEEDS_APPROVAL` (escalation.py) instead of entering T4/T5; the operator is asked with host, block kind, URL count, proposed tier and cost; approval raises that host's ceiling only. `CostLedger` refuses charges above the cap; `BRIGHTDATA_ENABLED` is the kill switch. |
| T6 | off; dual sign-off procedure in playbook §2 | `access_tier_ceiling` validator rejects `T6_POLICY_GATED` unless `authorization.reference` carries a `t6:` approval token with two approvers and an expiry. |
| Robots policy | `respect` for `public_third_party`; `informational` for `owner_authorized` | Default derived from `authorization.kind`; `respect_with_exceptions` requires `authorization.robots_exceptions` paths. |
| OCR languages | multilingual routing from Stage 1, local engines only | §6.1; unknown script → `UNSUPPORTED(ocr_lang)`. |
| Default interactions (2026-10-19, CR-009) | `scroll`, `expand_disclosure`, `select_tab`, `hover_info`, `open_informational_dialog`, `paginate_get` on by default; `keyboard_traverse` opt-in | `_default_actions()` in `contracts.py`. All run under the GET-only, no-navigation guard; a POSTing control is refused (`unsafe_side_effect`). With `open_informational_dialog` allowed, a consent overlay is answered "reject non-essential"; it is never accepted. |
| Bot-sensor beacons (2026-10-19) | keep blocking every non-GET request, telemetry included, in every tier up to T3 | Explorer guard and `TELEMETRY_HOSTS` (`explore/explorer.py`). An exception for a host's telemetry may be considered only for a host approved for T5 under the paid-tier procedure above, as part of that approval; nothing allows it today. |

---

## 20. Step 5 pre-answers (Security, rate-limit, financial)

1. **Targets and volume**: user-supplied hosts; ≤ 5,000 URLs × ~16 requests per run, governor-bound per host.
2. **Rate-limit keys**: `host:<host>` shared by fetch and render; vendor keys for paid routes.
3. **Worst-case spend**: $0 at T1–T3; T4/T5/cloud OCR/LLM capped by the run's `cost_ceiling_usd`; a 10,000-item batch cannot exceed the ceiling by construction.
4. **Idempotency**: all tasks keyed; no WRITE-class operations exist.
5. **Circuit breakers**: per host (governor) and per vendor (integrations).
6. **PII**: tagged per block; retention class per tenant; redaction in exports; raw evidence access-controlled.
7. **Input sanitization**: all scraped data enters through `StrictModel` contracts; page text is quoted data in prompts.
8. **Secrets**: `get_settings()` + `SecretStr`; workers receive short-lived per-task credentials only.
