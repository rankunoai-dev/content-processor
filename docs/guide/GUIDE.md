# Content Processor: A Plain-Language Guide

Last updated: 21 October 2026 · Code version: main after the struck-text change · Maintained by every session that adds a significant feature (see "Keeping this guide current" at the end)

## 1. What this is, in one paragraph

The Content Processor is an engine that visits a list of web pages, collects every piece of content on them (text, documents, images), and then tells you, page by page, how sure it is that it collected everything. Most tools that "scrape" websites quietly miss things: text hidden behind tabs, prices drawn as images, pages that block robots, documents behind forms. This engine is built to notice those gaps and say so plainly, with proof, instead of pretending a page was captured completely. It is built for Rankuno's SEO and content work, where a report is only useful if you can trust what it says is (and is not) there.

## 2. The problem it solves

Imagine asking an assistant to copy everything from 5,000 pages of a client's website. A careless assistant would hand back a pile of text and say "done". Some pages would be missing parts, some would be error pages, some would be a "please log in" screen copied as if it were the real page. You would never know which.

The Content Processor behaves like a careful assistant who:

- keeps a photocopy (evidence) of everything it looked at,
- writes down, for every page, what it could and could not get, and why,
- never counts a page as finished just because it loaded,
- is polite to the website (does not overload it, follows its rules),
- spends no money unless you explicitly approve it.

## 3. The ideas you need to know

### Evidence

Every page, document, screenshot and piece of text the engine collects is stored exactly as it was received, labelled with a unique fingerprint. Anything the engine reports can be traced back to these stored originals. If the stored copy is later missing or changed, the engine notices.

### Verdicts (badges)

Each web address gets exactly one verdict, chosen by strict rules:

| Verdict | What it means for you |
|---|---|
| VERIFIED_WITHIN_SCOPE | Everything the engine was allowed to look at was captured and checked. The only positive verdict. |
| NEEDS_REVIEW | Content was captured, but something deserves a human look (for example placeholder "Lorem ipsum" text left on a live page, a page that looks like a disguised "not found" page, or text that could not be confirmed). |
| PARTIAL | Some content was captured, some was not (for example part of the page is behind a form, an image could not be read, or a budget ran out). |
| BLOCKED | The website refused access (anti-robot challenge, login wall, age gate, wrong file served). |
| FAILED | The page could not be fetched at all after retries. |
| EXCLUDED | Deliberately not collected (the site's robots rules forbid it, it is outside the agreed scope, or the site demands payment). |
| RUNNING | Work for this page is still in progress. |

### Gaps

A gap is a written note explaining exactly what is missing and why, for example "image text not extracted: no OCR engine" or "form wall in front of the report". Gaps are what make the verdicts honest: a page can only be VERIFIED when it has no gaps that matter.

### Nothing disappears

Every address you supply ends up in the results, even the ones that were invalid, duplicated, forbidden or blocked. The engine never quietly drops a page from the totals.

## 4. A walk through one run, step by step

### Step 1: You describe the job (the "scope")

You give the engine a list of web addresses and a short description of the job: which websites are allowed, whether you own them or they belong to someone else, which languages and countries apply, and limits such as maximum time or maximum number of requests. For a public third-party website, the engine follows its robots rules strictly.

### Step 2: Planning

The engine cleans the list: it removes tracking codes from addresses, spots duplicates (so the same page is fetched once, with all original spellings kept as aliases), rejects addresses it must not visit (for example internal company networks), and puts important pages first.

Optionally, the engine can also group pages that share the same design template (for example all service pages of a site). Shared files such as stylesheets, fonts and scripts are then fetched once per group instead of once per page, which sends fewer requests to the client's website. Every page is still fetched and judged on its own.

### Step 3: Being polite and getting access

Before visiting a website, the engine reads its robots.txt rules and waits the delay the site asks for. If the site starts refusing requests, the engine slows down, and if refusals pile up it pauses that site completely for 15 minutes (a "circuit breaker") rather than hammering it.

Access works like a ladder, cheapest and gentlest first:

| Rung | What it is | Cost |
|---|---|---|
| T1 | A plain, polite download | Free |
| T2 | A download that looks exactly like a normal Chrome browser | Free |
| T3 | A real browser that opens the page like a person would | Free |
| T4 / T5 | Bright Data proxies or unblocking service | Paid: only after you approve it for that specific website, with spending caps |
| T6 | Special access | Off; needs two people's written sign-off |

The engine climbs the ladder only as far as needed. It never enters a paid rung on its own: it stops and asks.

### Step 4: Downloading and keeping the evidence

Each page is downloaded, its type is checked (for example, is this really a PDF, or an HTML login page pretending to be one?), and the original bytes are stored as evidence.

### Step 5: Reading the page's text

The engine reads the page's HTML and splits it into blocks (headings, paragraphs, tables), and labels each block by layer: visible content, hidden text, navigation and footer, third-party widgets such as chat boxes, icon codes, and so on. Hidden and decorative text is kept but never mixed into the main content.

Crossed-out text is kept and marked. A sale price shown as a struck-out "$200" next to "$150" is recorded as "Price ~~$200~~ $150", so a reader can see which price is current; the contradiction report never treats the crossed-out price as something the page still claims.

### Step 6: Opening the page in a real browser and exploring it

Many modern pages only show their content after the browser runs them, and much content sits behind tabs, accordions, "Load more" buttons, tooltips, pop-up dialogs, carousels and infinite scrolling. The engine opens each page in a real browser and explores it within strict rules:

- it only performs harmless, read-only actions (clicking tabs, expanding sections, scrolling, hovering);
- it never submits forms, logs in, buys, signs up or follows links away from the page;
- cookie banners are answered with "reject non-essential", never "accept";
- background tracking and "bot sensor" messages are blocked.

The engine then compares what the browser shows with what the raw HTML contained, so content that appears only after loading, or disappears, is noticed.

### Step 7: Documents and images

Linked PDFs, Word, Excel and PowerPoint files, and images are downloaded too (one link deep). PDFs are read including tables that continue across pages, form fields, comments and hidden layers. Scanned pages and images are read with OCR (optical character recognition: software that reads text from pictures). The OCR engine is Tesseract, which runs on the local machine for free, with 25 core languages (including English, Hindi, Gujarati, Tamil, Arabic, Chinese, Japanese and the main European languages).

### Step 8: Checking (the "verification brain")

Before giving a verdict, the engine runs a set of checks on each page. Each check writes a gap when something is wrong:

| Check | In plain words |
|---|---|
| Inventory (R1) | Was everything discovered on the page accounted for? |
| Access (R2) | Was the page (and its parts) actually obtained, or blocked or failed? |
| Frames (R3) | Were embedded frames inside the page also read? |
| Visual text (R6) | Does every line of text visible on the screenshot appear in what was extracted? |
| Channel agreement (R7) | Do the raw page and the browser agree? Is the text disguised with scrambled fonts? |
| PDF images (R8) | Were images inside PDFs read, and does the OCR agree with the PDF's own text? |
| Critical facts (R9) | Were numbers, prices, percentages, "not" and footnote marks preserved exactly? |
| Payload sanity (R10) | Is this really the page, or a challenge page, disguised "not found" page, redirect to the homepage, cut-off download or wrong file type? |
| Degradation (R11) | Is this page suspiciously thinner than the site's other pages (a sign of hidden blocking)? |
| Limits (R12) | Did exploring the page run out of its allowed time or actions? |
| Hygiene (R13) | Did icon codes or chat-widget text leak into the main content? |
| Settling | Did the page ever stop changing (live tickers and rotating content are flagged)? |
| Evidence integrity | Do the stored originals still exist and match their fingerprints? |

The engine also recognises "gates" on a page: lead-generation forms in front of a report, age or professional-only gates, login prompts, delivery pincode pop-ups, "product unavailable" notices and subscription paywalls.

### Step 9: The verdict

All gaps for a page are combined into one verdict using the strict order in section 3. A page is VERIFIED only when nothing stands in the way. The detailed status is always kept alongside, so a verdict never hides the reasons behind it.

### Step 10: The reports

At the end of a run the engine writes a bundle of files:

| File | What you use it for |
|---|---|
| R1 URL content (Excel, CSV) | One row per address: verdict, word count, gaps, and the whole extracted text. Long text is split across columns so Excel never cuts it off. |
| Review queue (Excel, CSV) | The pages that need a human decision, most serious first, with a blank column for the reviewer. |
| R2 overlap (Excel, CSV) | Groups of pages that share the same text (duplicate or near-duplicate content), with the shared passages quoted. |
| R4 contradictions (Excel, CSV) | Pairs of statements across the site that may contradict each other (for example two different prices for the same plan), with both quotes and a reviewer column. Pairs whose context differs (different years, countries or products) are listed separately, not as contradictions. |
| URL Content Explorer (web page) | An offline page to browse every address, its verdict, its gaps and its content. |

### The analyst web app

Besides the files, there is a web app for reviewing runs in a browser on your own computer (it only reads results and cannot change anything). It shows all runs; an overview of each run with charts of verdicts, the most common reasons pages were not verified and what to do about them; a searchable, filterable table of pages; a detail view per page (its content, issues, facts and technical details); duplicate content; possible contradictions; the pages that need your review; downloads of every report; and a dark mode.

## 5. Safety and cost promises

- Read-only: the engine never submits forms, logs in, buys, unsubscribes or clicks anything that changes the website.
- Polite: robots rules and requested delays are respected; a struggling site is paused, not hammered.
- Free by default: every run starts at $0. Paid access (Bright Data) needs your explicit approval per website, with caps of $5 per run and $25 per day, a monthly cap and a kill switch.
- Private networks are off-limits: the engine checks every address before contacting it, at every redirect. The basic downloader also connects only to the exact public address it checked, so a trick that swaps the address mid-way cannot redirect it into a private network (the browser and the Chrome-like downloader re-check each request instead).
- Evidence first: originals are stored before anything is changed, so every claim can be proven.

## 6. Results so far

| Test | Result |
|---|---|
| Live John Hancock website, 34 pages (19 October 2026) | 32 of 34 pages VERIFIED_WITHIN_SCOPE at $0 with the checks available that day. The other 2 correctly flagged NEEDS_REVIEW because the live pages contain "Lorem ipsum" placeholder text. Before the browser-exploration work, all 34 were PARTIAL. |
| Same website, re-run with the stricter checks and OCR (20 October 2026) | 3 of 34 VERIFIED. This is the more honest number: the new channel-agreement check found text that exists in the page code but was not captured from the browser on 21 pages (frequently-asked-question answers on the form pages, half of one press release), so those pages were less complete than the earlier run suggested. On 8 pages a text-joining fault introduced on 20 October glues words together; it is being fixed. 2 pages still contain placeholder text. |
| Adversarial test suite (28 trick pages) | Zero pages wrongly marked VERIFIED. Each trick (fake PDFs, challenge pages, form walls, paywalls, disguised fonts, soft redirects) gets the expected verdict. |
| Automated tests | 771 tests passing, 93.7% of the code exercised by tests. A CI pipeline runs all quality checks and the release gate on every change once the code is on GitHub. |
| Contradiction report on John Hancock | After the 20 October fix: 0 false contradictions (previously 17); differing-context pairs listed separately. |

## 7. What it does not do yet

- Paid access ladder: the Bright Data connection, spending ledger and approval command are not built yet, so a site that blocks all free access ends BLOCKED and the engine asks for approval.
- Durable database: the PostgreSQL storage exists but has never been tested against a real database.
- Starting runs from the web: the analyst web app reviews finished runs, but runs are still started from the command line, and there is no live progress feed or deployment package yet.
- Some content types: text drawn on canvas or in CSS, product variant selectors, audio and video, archives and email files, and old-style .doc and .xls files.
- Semantic similarity: finding pages that say the same thing in different words (planned with a vector database).
- Storage size: each explored page currently stores about 27 MB of evidence, which needs compressing before 5,000-page runs.
- Hidden page text: some text present in the page code (for example frequently-asked-question answers) is not yet revealed and captured by the browser exploration; the engine flags these pages for review rather than counting them complete.
- A text-joining fault (20 October) glues neighbouring words on some live pages; until it is fixed those pages are flagged for review.

## 8. Glossary

| Term | Meaning |
|---|---|
| Badge / verdict | The single summary label each web address receives. |
| Gap | A written note about something missing, with the reason and proof. |
| Evidence | The stored original bytes (page, document, screenshot) behind every claim. |
| Scope | The agreed description of a job: addresses, allowed websites, limits. |
| robots.txt | A website's published rules for automated visitors. |
| Circuit breaker | An automatic pause on a website that keeps refusing requests. |
| Access ladder (T1–T6) | The sequence of ways to reach a page, from free and gentle to paid. |
| OCR | Software that reads text from images and scanned pages. |
| Rendering | Opening a page in a real browser so its scripts run. |
| Exploration | Clicking tabs, expanding sections and scrolling to reveal hidden content. |
| Fixture | A test page built on purpose to trick the engine. |
| Release gate | The test that must pass before release: no page may be wrongly marked VERIFIED. |

## 9. Change log

| Date | Significant change |
|---|---|
| 2026-10-21 | Crossed-out text (for example an old sale price) is kept and marked with ~~ instead of reading as a current price. |
| 2026-10-20 | Analyst web app for reviewing runs in a browser. Optional grouping of pages by design template, so shared files are fetched once per group. |
| 2026-10-20 | Image and scanned-text reading switched on (Tesseract with 25 languages, found automatically). The visual-text check was tuned on the live John Hancock site so ordinary reading errors and cookie banners no longer cause false alarms. Images and PDFs no longer wait for a browser exploration they cannot have. Automated quality pipeline (CI) added. Live re-run: 3 of 34 verified, with the stricter checks finding real missing text. |
| 2026-10-20 | Six safety and accuracy fixes (failed image reading no longer reported as verified; compressed pages decoded correctly; paused websites resume; paid access always needs approval; connections locked to the checked address; contradiction report no longer confuses dated snapshots or different products). Critical-fact check (R9) added. Tesseract OCR installed with 25 languages. |
| 2026-10-19 | Browser exploration reaches 32 of 34 verified on John Hancock. Checks R6–R8 and R11–R13, page settling and evidence integrity added. Contradiction report (R4), paywall detection, run budgets, robots crawl-delay saved, stuck tasks reported. Office documents (Word, Excel, PowerPoint) supported. 222-case test inventory. |
| 2026-10-16 | Per-page verdicts with evidence, first checks (R1–R3, R10), reports (R1 content, review queue, overlap), offline explorer, run engine and command line. |
| 2026-10-15 | First version: planning, polite downloading, block detection, page reading, browser rendering. |

## 10. Keeping this guide current

This guide must be updated whenever a session or chat adds a significant feature (a new capability, a new check, a new report, a change to safety or cost rules, or new results). To update it:

1. Edit this file: docs/guide/GUIDE.md (plain text; keep the language non-technical).
2. Update the "Last updated" line, the relevant section, and add a row to the change log.
3. Rebuild the Word file by running: python scripts/build_guide.py
4. Commit both docs/guide/GUIDE.md and Content_Processor_Guide.docx with the feature.
