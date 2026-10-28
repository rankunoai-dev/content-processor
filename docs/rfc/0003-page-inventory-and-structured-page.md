# RFC 0003: Page inventory and structured page (SEO and GEO extraction)

Status: draft for discussion · 2026-10-28 · research only, nothing built yet

## 1. The ask

Today the engine saves the **text a visitor can read** (plus collapsed, hidden and
metadata layers, each kept apart). The ask is to also save **everything on a page that
matters for SEO and GEO** (links, images, media, metadata, structured data, technical
signals), and to save it **in the shape the page has on screen**, so that an e-commerce
page with categories, filters, tabs and content behind buttons comes out as an ordered
outline rather than a heap of text. That outline should be good enough to hand to Claude
or GPT, or a local model, which can then work with it directly.

## 2. What we already have (and where it stops)

| Already captured | Where | Gap |
|---|---|---|
| Visible text, collapsed panels, hidden text, accessibility-only text, each as a separate layer | `ContentBlock.layer` | none |
| Where each block sits (header, hero, mega menu, tab panel, accordion, carousel, card, modal, footer...) | `ContentBlock.ui_region` | none |
| Tabs, accordions, "show more", carousels clicked open; each opened view is a *state* | `explore/`, `State.action` | the link *control label → revealed content* is not exported as a tree |
| Title, meta description, canonical, `lang`, hreflang, Open Graph, Twitter, robots meta | stamp (`PageStamp`) | Twitter cards, `max-snippet`/`noai` directives, favicon, preload hints missing |
| Schema.org summary (types, products, offers, FAQ questions, breadcrumbs, dates) | `SchemaFacts` | the full JSON-LD is not kept; no validity check |
| h2/h3 outline, CTAs, numbers, word count, reading ease | `PageStamp` | no h1 count, no skipped-level check |
| Site menus as trees (header, footer, breadcrumb) | `SiteStamp.menus` | none |
| **Every link, image, iframe, video** | found in `adapters/html_source.py` (`DiscoveredLink`, `DiscoveredAsset`) | **used to plan the crawl, then thrown away.** Alt text, link position, `rel`, image size and lazy loading are never stored |
| ETag, Last-Modified, status, redirects | `UrlRecord` | other response headers (`X-Robots-Tag`, `Content-Language`, `Link`, cache, security) not kept |

So most of the raw material is already in our hands during a run; the work is to **keep
it, organise it and report it**, not to fetch more.

## 3. What SEO and GEO work needs from a page

GEO studies so far are early, and most of the percentages published in 2026 come from
vendors and are not independently repeated. The signals below are the ones that both
classic SEO and the GEO research agree matter. Answer-first structure, question
headings, structured data, semantic HTML, freshness and crawlable (non-JavaScript) content
come up in every source.

### 3.1 Links (per page)
- target URL, **anchor text**, `title`, `rel` (nofollow, sponsored, ugc), `target`
- internal / external / subdomain; same-page anchor; download (PDF etc.); mailto / tel
- **position**: ui region (header, footer, main, sidebar, card...), heading it sits under, order
- visible, collapsed (inside a tab or accordion), or hidden
- site level (no extra requests): inlinks per page, orphan pages, anchor-text spread per
  target, links pointing at redirects or at pages we saw fail
- optional (extra requests, off by default): status of every linked URL (broken links)

### 3.2 Images and media
- image URL, `srcset`/`<picture>` sources, **alt text** (missing / empty / decorative /
  file-name-like), `title`, caption (`<figcaption>`), width and height attributes,
  rendered size, `loading="lazy"`, format, file size when fetched, position and heading
- whether it is the hero or LCP-candidate image, Open Graph image
- text inside images (we already OCR images when asked)
- video: provider (YouTube, Vimeo, self-hosted), title, duration, thumbnail, transcript or
  captions track, `VideoObject` schema present
- iframes and embeds (maps, forms, widgets) and whether their content was readable

### 3.3 Head and technical metadata
- title and description **length** (characters and pixels), duplicates across the run
- canonical (self, other page, cross-domain, missing), `robots` meta and `X-Robots-Tag`,
  including `noindex`, `nosnippet`, `max-snippet`, `noimageindex`, `noai`/`noimageai`
- hreflang set and its return links; `lang` vs detected language
- Open Graph and Twitter cards complete or not; favicon; viewport
- response headers: status, redirect chain, `Content-Type`, `Content-Language`,
  `Last-Modified`, cache headers, `Link` headers
- page weight, request count and load timings from our own browser visit (lab numbers,
  not Google's field data)

### 3.4 Structured data
- the **full JSON-LD, microdata and RDFa** as found (not only the summary)
- per item: type, required and recommended fields present or missing (Google's rich-result
  requirements for Product, Offer, AggregateRating, Review, FAQPage, HowTo, Article,
  BreadcrumbList, Organization, LocalBusiness, VideoObject, Event, JobPosting)
- **mismatch check** between schema and visible text (schema price vs shown price, schema
  FAQ vs on-page FAQ, schema date vs shown date). AI systems and Google both penalise
  markup that disagrees with the page.

### 3.5 Content structure (the GEO core)
- h1 count, heading tree, skipped levels, **question-phrased headings**
- **answer-first**: does the first ~100 words under each heading answer it
- FAQ / Q&A blocks, how-to steps, definition lists, tables, comparison tables, lists
- statistics, figures and dates in the text; **outbound citations** to sources
- author: name, bio, link, credentials (E-E-A-T); reviewer; organisation
- published and updated dates, shown and in schema
- **JavaScript dependency**: share of the main text that is missing from the raw HTML and
  only appears after the browser runs scripts. Many AI crawlers do not run scripts, so
  such content may be invisible to them. We already keep the source and rendered layers
  separately, so this costs nothing extra and few tools can measure it.
- content hidden behind clicks (tabs, accordions, modals), as a share of the page

### 3.6 Site-level GEO files (once per site, a few small requests)
- `robots.txt` rules for each AI crawler, separated into **training** bots (GPTBot,
  ClaudeBot, Google-Extended, CCBot...) and **search/answer** bots (OAI-SearchBot,
  Claude-SearchBot, PerplexityBot...); blocking the search bots removes citations
- `llms.txt` / `llms-full.txt` present and what it lists (a proposal, not a standard;
  no ranking effect is proven)
- XML sitemaps: listed in robots.txt, URL counts, `lastmod` quality, our pages in or out
  of the sitemap

### 3.7 E-commerce specifics
- **Listing / category pages**: product grid as rows (name, URL, price, struck-out price,
  badge, rating, review count, image, availability); **facets/filters** as groups with
  options and counts and the URL each option produces (query string or path); sort
  options; pagination type (numbered, "load more", infinite scroll) and how many pages
- **Product pages**: price and currency, sale price, availability, SKU/GTIN/MPN, brand,
  **variant matrix** (size × colour → price/stock/URL), spec table, reviews summary and
  rating, Q&A, shipping and returns (often in tabs), delivery estimates, bundles and
  related products, breadcrumbs
- the schema-vs-visible check matters most here (price, availability, rating)

Filter combinations are **not** clicked one by one (10 filters × 10 options explode into
millions of views). We record each filter and the URL it produces; the URLs then feed
the normal crawl under the existing scope and budget rules.

## 4. Proposal

Two new outputs per page, both built from what a run already holds (stored HTML and the
rendered DOM snapshots in the evidence store), so **no extra visits** except the
site-level files in 3.6.

### 4.1 Page inventory (`<run>.inventory.jsonl`, one line per page)
The flat facts of section 3: links, images, media, head, headers, full structured data,
schema checks, site files. Each item carries its ui region, heading path, visibility
layer, state (which tab or click revealed it) and evidence id, like every other fact in
the engine. Excel sheets: *Links*, *Images*, *Media*, *Metadata*, *Structured data*,
*Issues*.

### 4.2 Structured page (`<run>.structured.json` and one Markdown file per page)
An ordered tree that mirrors the page as a visitor sees it:

```
Page: title · URL · published · updated · author · summary
├─ Breadcrumb: Home › Printers › Multifunction
├─ Hero: heading, text, image(alt), CTA "Get a quote" → /contact
├─ Section "Specifications"  (h2)
│   └─ Tabs
│       ├─ Tab "Overview"      → text, list, image
│       ├─ Tab "Tech specs"    → table (rows × columns)
│       └─ Tab "Downloads"     → links (PDF, size)
├─ Section "FAQ"
│   └─ Accordion: question → answer (× n)
├─ Product grid
│   ├─ Filters: Brand [Xerox (12), HP (8)] · Speed [...]
│   └─ Products: name · price · was-price · rating · URL (× n)
└─ Related links / footer menu (referenced, not repeated)
```

The tree is built by code from data we already have: `ui_region` and `heading_path` on
every block, explore *states* that record which control (tab label, button text) opened
which content, and the DOM snapshot for tables, lists and grids. The Markdown rendering
is the format in the screenshot the user shared (title, summary, URL, date, author,
then clean sections), with tabs and accordions shown as labelled sub-sections, tables as
tables and images as `![alt](url)`.

Repeated chrome (header, footer, cookie banner) is stored once per site (we already
have `SiteStamp.menus`) and only referenced from each page, so the tree stays readable.

### 4.3 Language models: optional, never required
The tree is deterministic, so any model, including Claude or GPT, gets clean input. A
model can then be asked for extra structure (for example "turn this spec table into
attributes"), but every value it adds must quote the source text, the same rule as the
offering catalog. Client content goes only to local or private-network models until the
data policy is confirmed (current rule, enforced in code).

### 4.4 GEO readiness checks (report only, no score invented)
Plain checks per page, each with the evidence: answer-first under headings, question
headings, FAQ present and marked up, author and dates present, schema valid and matching
the page, content needing JavaScript, content hidden behind clicks, AI search bots
allowed, images without alt text, statistics and sources cited. No single "GEO score"
until we can test one against real citation data (SE Ranking and Semrush, which we have
connectors for, report AI-search visibility).

## 5. Plan, smallest useful step first

| Step | What | Extra requests | Size |
|---|---|---|---|
| A | Page inventory: links, images, media, head, headers, full JSON-LD; Excel sheets | none | small |
| B | Structured page tree and Markdown per page (blogs, service pages, tabs, accordions, tables) | none | medium |
| C | Site files: robots.txt AI bots, llms.txt, sitemaps; internal link graph | a few per site | small |
| D | E-commerce: product grids, facets, variants, product facts, schema-vs-page check | none (facet URLs join the crawl under budget) | medium-large |
| E | GEO readiness checks and report | none | small |
| F | Optional: broken-link status checks | one light request per linked URL | small, off by default |

Each step follows the usual rules: guide update, full test gate, lane-log entry.

## 6. What is out of scope

- Google's real-user speed data (Core Web Vitals field data needs the CrUX API) and
  rankings or backlinks (the Semrush / SE Ranking connectors cover those).
- Logging in, filling forms or checking out to reveal content (existing gated-content rules
  apply).
- Clicking every filter combination (see 3.7).

## 7. Open questions

1. Which first: A+B (any site, e.g. the Flynn's blog posts), or D (e-commerce)? If D, which
   site do we test on?
2. Is step F (one extra request per linked URL to find broken links) wanted?
3. Should the Markdown per page become the default "content" file a client receives?

## Sources

- GEO16 framework, AI answer-engine citation study: https://arxiv.org/pdf/2509.10762
- AI crawler user agents, training vs search bots: https://hyperleap.ai/blog/top-llm-crawlers-and-what-they-do-2026 ,
  https://kinsta.com/blog/llms-txt-vs-robots-txt-wordpress/
- Google Product structured data (merchant listings, variants):
  https://developers.google.com/search/docs/appearance/structured-data/product ,
  https://developers.google.com/search/blog/2022/09/merchant-listings
- GEO tactics overview (vendor, unverified figures): https://www.airops.com/blog/generative-engine-optimization-geo
