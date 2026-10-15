# Feasibility on Large Targets and the Gated-Content Policy

> **Document ID**: `CP-DOC-FEASIBILITY-V1`
> **Status**: Binding policy for gated content; feasibility figures are planning estimates, not measurements
> **Date**: 2026-10-16
> **Companion**: `ACCESS_RESILIENCE_PLAYBOOK.md` (tiers, caps, approval), `ARCHITECTURE.md` section 10 (verification), gap audit cases 141–149 and 215–222

## 1. The question, answered without marketing

"Can we extract 90% of the content of Amazon, Flipkart or GEP?" has two different answers depending on what "all the URLs" means.

| Target | Meaning of "all" | Feasible at | Tier needed | Honest expectation |
|---|---|---|---|---|
| **GEP** (B2B corporate site, ~2k–5k public pages) | the whole public site | yes | T1–T3, $0 | **90–97% of public pages verified**. The remainder is gated assets (whitepapers behind forms), dynamic calculators and embedded third-party viewers, all of which the engine reports as typed gaps rather than silently missing. |
| **Amazon / Flipkart**, a **declared list** of 500–20,000 product or category URLs | that list | yes, with escalation | T3 locally for a while; T4/T5 (Bright Data, capped) as soon as bot management reacts; sticky residential sessions | **85–95% of listed URLs acquired with main product content verified** once a paid tier is approved for that host. At T1–T3 from one IP, expect challenges after tens to a few hundred requests and a correspondingly lower number. |
| **Amazon / Flipkart**, the **entire catalogue** | every product, variant, review page | no | none at any sane cost | Not a crawl problem. Hundreds of millions of ASINs, billions of URLs with variants and pagination, aggressive bot management, and terms of use that prohibit it. Nobody "downloads the database". |

How the known Amazon-intelligence vendors actually get data is instructive: they prioritise the best-selling tail (Pareto), use the official Product Advertising and Selling Partner APIs where permitted, and run browser extensions through which real shoppers' page views feed their servers. Our engine is not that product. It is a scoped, evidence-backed extractor: give it a list, and it tells you exactly which URLs it got, at which tier, and why the rest did not come.

### What changes the number on e-commerce hosts

- **Session identity matters more than IP count.** One clearance cookie per sticky session serves many pages; rotating identities per request is what gets a pool burned. The host governor already keeps one session tuple per host and escalates per host, not per request.
- **The content is behind state, not behind HTML.** Pincode/delivery modals, variant selectors that change price, "see more" expanders, lazy review and Q&A pagination, A+ content loaded on scroll. The state explorer (Stage 2) is what moves product pages from "acquired" to "verified"; without it the honest badge is PARTIAL with `UNVISITED_STATE` gaps.
- **Terms of use**: Amazon's and Flipkart's conditions prohibit automated access. Public data scraping has survived in court in some jurisdictions and not in others; this is an engagement-level legal decision recorded in `scope.authorization`, not an engineering default. The playbook's legal gate applies.

### Cost envelope for a paid rung (planning, to be measured)

With Bright Data Web Unlocker at list prices, 5,000 product pages at T5 cost in the low tens of dollars; residential proxy traffic for the same pages is of the same order. That is why the per-run cap is $5 by default: a test batch of a few hundred pages tells us the success rate before anyone commits more.

## 2. Will the engine know the real reason a page was dismissed?

Yes, and it must be able to prove it. A naive scraper logs "timeout" or "extraction failed". This engine records a typed verdict, a typed gap, and a pointer to the bytes and the screenshot.

| What happened | What the engine detects | Status | Gap type and reason | Evidence linked |
|---|---|---|---|---|
| Cloudflare challenge, Turnstile | `cf-mitigated: challenge`, challenge platform script, Turnstile frame | BLOCKED | `ACCESS_BLOCKED` / `js_challenge:cloudflare` or `interactive_captcha:cloudflare` | raw bytes, rendered DOM, screenshot |
| Akamai / Imperva / DataDome / PerimeterX wall | vendor headers, cookies, reference-id body | BLOCKED | `ACCESS_BLOCKED` / `fingerprint:<vendor>` | same |
| Product unavailable or unlisted (HTTP 200) | "currently unavailable", "out of stock", "no longer available" in the main region | NEEDS_REVIEW | `DEGRADED_VARIANT` / `product_unavailable` | rendered DOM, screenshot |
| Delivery pincode / location modal | overlay with a pincode/zip input and gating text | PARTIAL | `UNVISITED_STATE` / `geo_pincode_modal_gated` | screenshot of the modal |
| Lead-generation form wall (whitepaper, report, guide) | form with an email input and a download/access call-to-action, asset words on the page, little other main content | PARTIAL (teaser kept) or BLOCKED (nothing else) | `ACCESS_BLOCKED` / `lead_gen_form_wall` | screenshot of the form, rendered DOM |
| Login wall rendered in the DOM | password input in a form that dominates the page | BLOCKED | `ACCESS_BLOCKED` / `login_wall` | screenshot |
| Age / professional gate | "confirm you are over 18", "healthcare professional" dialog | BLOCKED | `ACCESS_BLOCKED` / `age_or_audience_gate` | screenshot |
| Placeholder page published live | "Lorem ipsum" in the visible corpus | NEEDS_REVIEW | `DEGRADED_VARIANT` / `placeholder_content_suspected` | rendered DOM |
| Infinite scroll or action budget exhausted | scroll passes or actions hit the scope limit | PARTIAL | `BUDGET_STOPPED` / `max_scroll_passes` | state snapshots |
| Page never settles | text/node deltas never quiet inside the window | NEEDS_REVIEW | `VOLATILE` / `settle_window_exhausted` | settle series |

Every row is a fixture in the adversarial suite or a scheduled addition (section 5). The classifier for HTTP-level blocks lives in `governor/block_classifier.py`; the DOM-level gate detector in `governor/gates.py`.

## 3. The gated-content policy (what we do and do not do)

Faking a form submission is not on the table. It fails on business-email validation, it rarely yields the asset (the PDF is emailed, not displayed), and it floods the target's CRM, which is both a legal exposure under anti-spam rules and the fastest way to get a client's domain blacklisted. The engine offers three paths. The first is the default and always runs; the other two are per-engagement decisions.

```
          rendered page ─► gates.py detects lead_gen_form_wall
                               │
                               ├─ always: extract the teaser (title, abstract, summary),
                               │          record Gap(ACCESS_BLOCKED, lead_gen_form_wall),
                               │          link the screenshot, badge the URL "gated asset"
                               │
                 ┌─────────────┴─────────────┐
                 ▼                           ▼
     PATH A: authorized persona       PATH B: HITL review queue
     (scope.persona, signed off)      (always available)
     fills the form with a real,      exports review_queue.jsonl; an
     consenting corporate identity;   analyst obtains the asset and
     IMAP listener collects the       drops it into evidence; the
     emailed link; asset ingested;    URL becomes VERIFIED_EXTRACTED
     gap closed as resolved_by_persona  with resolved_by_operator
```

### Path 0: honest gap (default, zero risk)

Implemented. The teaser blocks are kept in the corpus with their layers, the gap carries the reason and the screenshot, and the whole-content export shows the teaser with the gap noted. Downstream analysis sees exactly what was public.

### Path A: authorized corporate persona (opt-in, signed off)

Contract implemented (`RunScope.persona`); the form filler and the inbox listener are Stage 2b and run only when the contract validates:

- A real, consenting person's business identity (no free-mail domains, no invented names), with a `consent_reference` recorded in the scope.
- The engagement's authorization kind is owner-authorized or licensed, **or** the AI Lead has approved third-party persona use for that engagement (the reference goes in `scope.persona.approval_reference`).
- Rate: at most one submission per host per day by default, never on forms whose call-to-action is "subscribe", "contact sales" or anything that creates an obligation.
- Every submission is an event with the form fields sent (secrets redacted) so the client can audit what was submitted in their name.

### Path B: HITL review queue (always on)

Implemented for dry runs as an export; for durable runs through the `resolve` command:

1. The run writes `<out>.review_queue.jsonl`: one line per gated URL with the reason, the teaser word count and the evidence ids (screenshot, DOM).
2. An analyst obtains the asset legitimately (their own login, a request to the vendor, a client-provided copy).
3. `cli resolve --run <id> --url <url> --file <asset> --note "<how it was obtained>"` stores the file as evidence, attaches it to the resource, closes the gap and sets the status to `VERIFIED_EXTRACTED` with reason `resolved_by_operator`. The note and the operator are in the event log.

## 4. What this means for the three target families

- **GEP-like B2B sites**: expect a handful to a few dozen gated assets per site. The review queue is the right tool; a persona is worth configuring only if the client wants those assets routinely.
- **Flipkart/Amazon-like marketplaces**: no lead forms, but pincode modals, variant states and unavailable products on a large share of URLs. Stage 2's state explorer plus a capped paid tier per host is the path to the 85–95% figure; the engine will show the number honestly as it climbs.
- **Any site with a login wall around the content you need**: BLOCKED, with the login page as evidence. The only compliant unlock is client-issued credentials (T0, `access_context=authenticated_client`).

## 5. Additions to the edge-case inventory (cases 215–222)

| ID | Case | Detection | Strategy | Residual outcome |
|---|---|---|---|---|
| 215 | Lead-generation form wall around an asset | email input + download/access CTA + asset words; low main-content word count | teaser kept; gap `lead_gen_form_wall`; review queue; persona only if signed off | gated asset listed with screenshot |
| 216 | Delivery pincode / location modal (marketplaces) | pincode/zip input in an overlay with gating text | gap `geo_pincode_modal_gated`; state explorer applies declared location if scope provides one | modal screenshot; content behind it unvisited |
| 217 | Product unavailable / unlisted with HTTP 200 | unavailability phrases in the main region | `DEGRADED_VARIANT` review gap; never treated as a normal product page | flagged |
| 218 | Sponsored and recommendation interleaving in listings | sponsored badges, recommendation rails | label `third_party_chrome` or `aside`; never merged into the listing corpus | labelled |
| 219 | Variant selectors that change price, stock and images | option groups with price deltas | state explorer enumerates declared finite options (case 43) | unvisited variants listed |
| 220 | Reviews and Q&A behind load-more and login | pagination controls; login prompts on later pages | GET-only pagination within budget; login = BLOCKED for that region | region-level gaps |
| 221 | Price or key facts rendered as images or obfuscated fonts | OCR vs DOM disagreement (case 114) | OCR reading retained; `FIDELITY_DISAGREEMENT` review | reviewed |
| 222 | Marketplace identity explosion (ASIN/PID canonical vs tracking and variant URLs) | canonical and variant parameters | schedule key keeps variant parameters; canonical recorded as a hint (cases 91, 97) | aliases listed |
