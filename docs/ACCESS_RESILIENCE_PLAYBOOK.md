# Access Resilience Playbook — Staying Unblocked Without Lying About It

> **Document ID**: `CP-DOC-ACCESS-V1`
> **Status**: Binding operating policy for the content-processor acquisition layer
> **Date**: 2026-10-14
> **Companion**: `ARCHITECTURE.md` §7 (Host Governor) and ADR-0004

## 0. The one-paragraph version

The strongest way to not get blocked is to be **allowed**. For client sites that is an allow-list rule and a self-identifying crawler. For public third-party sites it is looking and behaving like the ordinary visitor the site already serves, at a rate the site does not notice. Every rung above that costs money, adds legal exposure, and still ends in a `BLOCKED` ledger entry when it fails. The system climbs the ladder automatically **only as far as the run's declared ceiling**, records which rung acquired each resource, and never pretends a challenge page was content.

---

## 1. Authorization model (decides the ceiling)

| `authorization.kind` | Meaning | Default ceiling | Notes |
|---|---|---|---|
| `owner_authorized` | Rankuno has written permission from the site owner (client engagement) | `T0` | Allow-listing is requested as part of onboarding. Identify the crawler. Suppress analytics. Never need T4+. |
| `public_third_party` | Publicly served pages, no relationship | `T3` | Ordinary visitor behaviour. T4 needs an engagement-level flag. |
| `licensed_source` | API or feed under licence | `T0` | Use the licensed channel only. |
| `legal_hold` | Cease-and-desist, contract, or operator decision | none | `EXCLUDED` for every URL on the host. |

The `authorization` block on `RunScope` is mandatory. A run without it is rejected at admission.

---

## 2. The ladder

### T0 — Authorized access (client sites)

Strongest completeness and zero block risk. Onboarding checklist for every client engagement:

1. **WAF/CDN allow-list**: a custom rule on the client's Cloudflare/Akamai/AWS WAF that skips bot management for requests carrying a per-client secret header (`X-Rankuno-Crawl: <hmac>`) **or** originating from Rankuno's static egress IPs. Cloudflare: WAF custom rule with action *Skip* → Bot Fight Mode / Super Bot Fight Mode / Managed Challenge. Akamai: Bot Manager "known bot" exception. AWS WAF: IP set allow rule ordered first.
2. **Self-identifying User-Agent** on authorized sites: `RankunoContentBot/1.0 (+https://rankuno.com/bot; client=<slug>)`. This is what lets the client filter us in GA4 (bot filtering by UA / IP), Hotjar, and A/B tools.
3. **Analytics suppression**: the browser worker blocks `googletagmanager.com`, `google-analytics.com`, `analytics.google.com`, `hotjar.com`, `clarity.ms`, `segment.io`, `optimizely.com`, `vwo.com` and the client's declared tag domains. Scope flag `analytics_suppression=true`.
4. **Stronger denominators**: ask for the CMS export, sitemap index, DAM asset list, or a read-only CMS API token. An owner inventory is a better denominator than any crawl.
5. **Staging access** where production is sensitive to load.
6. **Authenticated areas**: only with client-issued credentials, stored as Playwright storage state, encrypted per tenant, never written to evidence or logs.

### T1 — Polite HTTP

Default for every host. The host governor enforces:

- Per-host concurrency 1 (2 on CDN-fronted hosts after 50 clean responses), adaptive delay = max(crawl-delay, p50 latency × 2, 500 ms), jitter ±30%.
- `robots.txt` fetched once per host per run, cached with RFC 9309 semantics (4xx = allow, 5xx/unreachable = disallow until retry). **Decided 2026-10-14**: `respect` is the default on `public_third_party` hosts (disallowed URLs become `EXCLUDED_BY_POLICY` with the matched directive as evidence); `informational` on `owner_authorized` hosts (directive recorded, not enforced, because written owner permission supersedes it); `respect_with_exceptions` only with an explicit path allowlist in the authorization record. Crawl-delay is honoured in every mode.
- `Retry-After` honoured in both formats; 429 → halve concurrency, double delay, resume after window; three 429s in 10 minutes → circuit OPEN for 15 minutes.
- Conditional GETs (`If-None-Match`, `If-Modified-Since`) on recrawl; `Accept-Encoding` with bounded decode.
- Shared assets fetched **once per content hash** across the whole run (a logo linked from 5,000 pages is one request).
- Sitemaps and feeds first: they are cheap, expected traffic, and often full-content.
- Off-peak scheduling per host timezone when the run is not urgent.
- Never more than one in-flight browser render per host unless authorized.

### T2 — Impersonated HTTP

Used when T1 returns a fingerprint-class block (403/429 on plain `httpx` but the same URL opens in a browser) **or** from the start on hosts known to run bot management.

- Fetcher: `curl_cffi` with `impersonate="chrome"` (JA3/JA4 and HTTP/2 SETTINGS/priority frames consistent with a real Chrome build). `httpx` stays for T1 and for internal/API calls.
- Header profile pinned per session: order, casing, `Accept`, `Accept-Language` consistent with scope locale, `Sec-CH-UA*` consistent with the impersonated Chrome version, plausible `Referer` (previous in-site page), `Sec-Fetch-*`.
- Cookie jar per host session; never rotate UA, TLS profile or IP inside a session (clearance cookies are bound to the tuple).
- Still inside the T1 rate envelope.

### T3 — Fidelity browser

Used for all pages that need rendering, and as the escalation when T2 is challenged.

- Playwright with the **real Chrome channel** (not bundled Chromium), new headless mode, or headed under Xvfb for hosts that detect headless.
- Context emulation consistent with the scope **and the egress IP**: locale, timezone, geolocation, viewport in common sizes, colour scheme.
- Anti-detection surface handled by a maintained fork rather than hand patches: `patchright` (Playwright fork that avoids CDP `Runtime.enable` leaks) or `camoufox` (Firefox-based). `playwright-stealth` is a last resort; randomized canvas/WebGL hashes are themselves a signal. Evaluate in Stage 1 against the fixture hosts; pin one.
- Humanized pacing inside the politeness envelope: scroll with easing and variable step, pointer moves before clicks, dwell times drawn from a distribution, no 0-ms action chains.
- Real asset loading (fonts, images) so the page's behavioural telemetry looks normal. Analytics domains blocked only on authorized sites.
- Persistent per-host session profile for the run (cookies, local storage) when the site uses clearance cookies; fresh context per URL otherwise.
- Browser subrequests go through the same egress proxy with the same IP as the session.

### T4 — Network identity (proxies)

Used when blocks correlate with IP class (datacenter ASN, shared ranges) or when geography must match the declared scope. `RiskClass.FINANCIAL`.

- Pool classes: `datacenter_static` → `isp_static` → `residential_rotating` → `mobile`. Prefer the lowest class that works; residential and mobile are expensive and ethically sensitive (consent-based networks only; vendor due diligence required).
- **Sticky sessions**: one IP per host session for the session's life; rotation only on block or expiry.
- **Geo-matching**: IP country must match `scope.geography`; `Accept-Language` and timezone must match the IP.
- **Tenant isolation**: separate sub-pools per client so one client's aggressive run cannot burn another's IPs.
- **Pool health**: per-IP block score, cooldown, retirement; per-ASN global request cap.
- Vendors are interchangeable behind one `ProxyProvider` interface (Bright Data, Oxylabs, Smartproxy/Decodo, IPRoyal, Webshare). Cost is charged to the run's `CostLedger` per GB.
- **Rankuno holds a Bright Data subscription.** It is the only configured vendor at launch: ISP/residential zones back T4 through `ProxyProvider("brightdata")`; Web Unlocker backs T5 through `UnblockerProvider("brightdata_unlocker")`; Scraping Browser (remote CDP endpoint that Playwright connects to) backs a T5 browser variant for challenge-heavy, JS-rendered hosts. Credit governance is in §10.

### T5 — Managed unblockers

Used when T4 still yields vendor challenges and the engagement allows it. `RiskClass.FINANCIAL`, mandatory HITL approval per engagement (Rankuno Step 3).

- Vendor "unblocker" APIs handle TLS, browser fingerprint, JS challenges and, under their own terms, challenge solving: Zyte API, Bright Data Web Unlocker, Oxylabs Web Unblocker, ScraperAPI, ZenRows, ScrapingBee. One `UnblockerProvider` interface; vendor chosen per host by measured success rate and cost.
- Self-hosted FlareSolverr (Cloudflare JS-challenge solver) for low-volume hosts; it is unmaintained-risk and must be sandboxed like any browser worker.
- Every response acquired at T5 is still run through the block classifier (§4): vendors return challenge pages too.

### T6 — Policy-gated (off by default)

Requires written sign-off by the AI Lead **and** the client, recorded in `scope.authorization.reference`:

- Third-party CAPTCHA-solving services (2Captcha, CapSolver, Anti-Captcha) for hosts that present interactive challenges.
- Accepting HTTP 402 pay-per-crawl pricing.
- Any login not covered by client-issued credentials.

Default behaviour at T6 boundary: `BLOCKED` with the challenge preserved as evidence.

**Dual sign-off procedure (decided 2026-10-14, T6 remains OFF by default):**

1. The engagement owner files a T6 request naming the host(s), the specific capability (CAPTCHA service, pay-per-crawl, or non-client login), the business reason, the expected volume and the cost cap.
2. Counsel or the AI Lead records the jurisdiction check from §7 against that host.
3. Two written approvals are required: the **AI Lead** and the **client's authorized signatory**. Both are stored with the engagement record.
4. The approval is encoded as `t6:<ticket>:<approver1>:<approver2>:<expires_at>` in `scope.authorization.reference`. The `RunScope` validator refuses `access_tier_ceiling = T6_POLICY_GATED` without a well-formed, unexpired token, and refuses it entirely while `cost_ceiling_usd = 0`.
5. Every T6 acquisition is logged to the audit JSONL with the ticket id; the run's export marks those resources `acquired_at_tier = T6_POLICY_GATED`.
6. Approval expires with the engagement or after 90 days, whichever is first.

**Operating policy (final, 2026-10-15)**: every run starts at `cost_ceiling_usd = 0` with ceiling T3, so T4, T5 and T6 are unreachable by default. Paid tiers are an escalation ladder, not a starting point:

1. A host blocks at T3 (or at T2 with a block that needs T4+). The governor returns `NEEDS_APPROVAL` with the proposed tier; the affected URLs are recorded `BLOCKED(tier_reached=T3)` and an event `paid_tier_available:<tier>` is emitted.
2. The operator is asked (chat, n8n notification, or the run page). Nothing is spent while the question is open.
3. On approval the operator raises the ceiling **for that host only**, with a small cap (default $5 per run, inside the $25/day and operator-set monthly caps of §10). Bright Data is the vendor. The approval is recorded in the run events with who approved and the cap.
4. The host's blocked URLs are re-queued at the new tier; results are compared against the T3 attempt so the team can see whether the paid rung actually acquired more.
5. If the cap is exhausted, the host returns to `BLOCKED` with `BUDGET_EXHAUSTED` evidence; no automatic top-up.

The question to the operator must state: host, number of blocked URLs, block kind and vendor, proposed tier, estimated cost at that tier, and the cap being requested.

### Never

These are refused by code, not by policy flags:

- Credential stuffing, credential sharing, or session hijacking.
- Exploiting WAF or application vulnerabilities to bypass controls.
- Retry storms, concurrency that degrades a target, or anything resembling denial of service.
- Bypassing paywalls, DRM, or access controls on non-authorized hosts.
- Ignoring a legal hold.
- Following action-like links (`logout`, `delete`, `add-to-cart`, `vote`, `unsubscribe`) or submitting forms on any host.
- Harvesting PII beyond the declared scope and retention class.

---

## 3. Escalation state machine (per host, per run)

```
                 clean responses
   ┌──────────────────────────────────────────────────┐
   ▼                                                  │
 [T_current] ──block classified──► classify(kind) ─────┤
   │                                                   │
   │ kind=rate      → backoff, same tier, circuit HALF-OPEN
   │ kind=transport → retry bounded, same tier
   │ kind=fingerprint (403 on HTTP, OK in browser) → T2/T3
   │ kind=ip_reputation (403 everywhere, ASN-correlated) → T4
   │ kind=js_challenge / managed_challenge → T3, then T5 if allowed
   │ kind=interactive_captcha → T5 if allowed, else T6 boundary → BLOCKED
   │ kind=auth_wall / paywall / geo_gate → BLOCKED (T0 path only)
   │ kind=legal / 402 / ai_optout(enforced) → EXCLUDED
   │ kind=shadow_block_suspected → escalate one tier, mark NEEDS_REVIEW
   │
   └─ tier > ceiling → BLOCKED with evidence and tier reached
```

Rules:

- Escalation is **per host**, sticky for the run, and decays back one tier after 200 clean responses.
- A tier change creates a new host session (new IP, UA, TLS profile, cookie jar). Never mix.
- Every resource records `acquired_at_tier`. Downstream analysis can weight by tier.
- Circuit breaker: 5 consecutive non-transient failures → OPEN 15 min → HALF-OPEN with one probe → CLOSED on success.
- Cooldown after escalation: 60 s minimum before the first request at the new tier.

---

## 4. Block classifier

Runs on **every** response, at every tier, including vendor-returned ones. Output: `BlockVerdict{kind, vendor, confidence, evidence}`.

### 4.1 Signals table

| Vendor / class | Header, cookie, status and body signals |
|---|---|
| Cloudflare | `cf-mitigated: challenge`; `server: cloudflare` with 403/503 and titles "Just a moment…", "Attention Required!"; error codes 1006/1008/1010/1015/1020 in body; cookies `__cf_bm`, `cf_clearance`; Turnstile frame `challenges.cloudflare.com`; HTTP 402 with crawl-pricing headers (pay-per-crawl) |
| Akamai Bot Manager | cookies `_abck`, `bm_sz`, `ak_bmsc`, `bm_mi`; body "Access Denied … Reference #18.xxxx"; 403 from `AkamaiGHost` |
| HUMAN (PerimeterX) | cookies `_px3`, `_pxhd`, `_pxvid`; 403 JSON with `blockScript`/`appId`; `px-captcha` element |
| DataDome | cookie `datadome`; header `x-datadome`; 403 JSON with `dd` object; `geo.captcha-delivery.com` frame |
| Imperva / Incapsula | cookies `visid_incap_*`, `incap_ses_*`; body "Incapsula incident ID"; `_Incapsula_Resource` script |
| Kasada | headers `x-kpsdk-*`; 429 with `KPSDK` in body |
| AWS WAF | header `x-amzn-waf-action: challenge`; 405 with `aws-waf-token` cookie; `challenge.js` |
| F5 Shape / Distributed Cloud | obfuscated `_imp_apg_r_` cookies; 403 with empty body and `TS` cookies |
| Sucuri | header `x-sucuri-id`; body "Access Denied - Sucuri Website Firewall" |
| Anubis (PoW) | HTML "Making sure you're not a bot!" with mascot; cookie `within.website-x-cmd-anubis-auth`; JS proof-of-work |
| reCAPTCHA / hCaptcha / Turnstile | frames `google.com/recaptcha`, `hcaptcha.com`, `challenges.cloudflare.com/turnstile` |
| Generic WAF | 403/406/429/503; HTML returned where non-HTML requested; body < 2 KB with "denied", "blocked", "forbidden", "unusual traffic"; `Retry-After` |
| Auth wall | 401; redirect to `/login`, `/signin`, SSO domains; `WWW-Authenticate` |
| Geo gate | 451; body "not available in your region"; redirect to country selector |
| Soft block (shadow) | 200 with empty JSON arrays where a canary expects items; main region missing; content length < 40% of host baseline; text scrambled (OCR ≠ DOM); prices replaced by placeholders |
| AI opt-out (not a block, a policy signal) | `robots.txt` disallow for AI agents; `<meta name="robots" content="noai">`; `tdmrep.json`; `X-Robots-Tag: noai`; content-signal headers |

### 4.2 Canary and dual-channel checks

- **Canary URL per host**: the first URL of each host is fetched at T1 and T3 and compared; large disagreement with no vendor marker → `SHADOW_BLOCK_SUSPECTED`.
- **Baseline drift**: host-level rolling median of main-region word count; a page under 40% of baseline with no 4xx/5xx → `DEGRADED_VARIANT` review.
- **Alternate-class probe**: on suspicion, one request from a different IP class; if content differs → IP-class block confirmed.

---

## 5. Footprint reduction (fewer requests, fewer reasons to block)

- Content-addressed fetch cache across the run and across runs (same hash, no refetch within `reuse_ttl`).
- Sitemap/feed/API acquisition first; HTML render second; asset fetch third via HTTP, not via the browser's own requests, when the browser has already cached the bytes (Playwright response bodies are captured and persisted, so images seen during render are never fetched twice).
- CMS-native endpoints as alternative channels when exposed and public: WordPress `/wp-json/wp/v2/`, Drupal JSON:API, Shopify `.json` product endpoints, Ghost Content API, Webflow CMS feeds. Recorded as a separate representation channel (`feed_or_api`), never substituted for the rendered page's verification.
- Archive channels for history and as a tie-breaker, never as the primary: Wayback Machine CDX, Common Crawl index. Recorded with capture timestamp of the archive.
- Block third-party trackers in browser on **authorized** hosts only.
- Never fetch `robots.txt`, favicons, or sitemaps more than once per host per run.

---

## 6. Identity consistency checklist (T2–T4)

A session is a tuple `(egress_ip, user_agent, tls_profile, http2_profile, header_order, locale, timezone, cookie_jar)`. Any change creates a new session. Checklist enforced by code before the first request:

- UA Chrome version == `Sec-CH-UA` version == TLS impersonation target.
- `Accept-Language` primary language == scope locale == browser `navigator.languages[0]`.
- Browser timezone == IP geolocation timezone (± proxy vendor's stated region).
- Viewport ∈ {1366×768, 1440×900, 1536×864, 1920×1080}; `outerWidth/outerHeight` non-zero.
- WebGL renderer string not `SwiftShader`/`llvmpipe` (use hardware or ANGLE-reported renderer).
- `navigator.webdriver` absent; no `HeadlessChrome` token in UA.
- No automation extension IDs; no exposed CDP globals.

---

## 7. Legal and ethical gate (per engagement, before any run)

This document is not legal advice; the following is the checklist counsel should confirm per engagement.

- Authorization kind recorded and signed.
- Target jurisdiction notes: computer-misuse statutes (CFAA and analogues), contract/ToS exposure, database rights (EU), TDM exceptions and opt-outs (EU DSM Art. 4), copyright for stored evidence, personal-data law (GDPR, India DPDP Act 2023) for PII encountered in pages, and any sector rules.
- Robots policy decided (respect / respect-with-exceptions / informational).
- UGC in or out; PII retention class.
- Proxy vendor due diligence (consent-based residential networks only).
- Ceiling tier and `CostLedger` ceiling approved.

---

## 8. Metrics the host governor exposes

Per host and per tier: requests, 2xx/3xx/4xx/5xx, challenge rate, shadow-block suspicions, p50/p95 latency, concurrency, delay, circuit state, escalations, cost. Alerts: challenge rate > 10% over 50 requests; circuit OPEN more than twice in an hour; any `legal_hold` host appearing in a frontier.

---

## 9. Vendor and tool table

| Rung | Tool | Risk class | Cost model | Notes |
|---|---|---|---|---|
| T1 | `httpx` | READ | none | HTTP/1.1 + HTTP/2; async |
| T2 | `curl_cffi` | READ | none | Browser TLS/H2 impersonation |
| T3 | Playwright + Chrome channel; `patchright` or `camoufox` | READ | none | Pin one fork after Stage 1 evaluation |
| T4 | Bright Data / Oxylabs / Decodo / IPRoyal / Webshare | FINANCIAL | per GB or per IP | Behind `ProxyProvider` |
| T5 | Zyte API / Bright Data Web Unlocker / Oxylabs Unblocker / ScraperAPI / ZenRows; FlareSolverr (self-hosted) | FINANCIAL | per request | Behind `UnblockerProvider`; HITL per engagement |
| T6 | CAPTCHA solvers; pay-per-crawl | FINANCIAL | per solve / per page | Off by default; dual sign-off |
