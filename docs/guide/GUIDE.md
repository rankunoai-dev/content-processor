# Content Processor: A Plain-Language Guide

Last updated: 29 October 2026 · Code version: main after the full shared text in the project report's matching sheet, product options and listing filters in browser exploration (opt-in), page outlines with page types, the SEO and GEO inventory, the unified URL history across runs, page stamping and offering catalog · Maintained by every session that adds a significant feature (see "Keeping this guide current" at the end)

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

When a download meets a bot check (for example Cloudflare's "Just a moment..." page), the page is handed to the real browser (T3) instead of being given up. If the browser gets through, the page is read and checked like any other; if the browser is stopped too, the page is reported as blocked, with the rung where it happened, and paid access can be approved for that website.

### Step 4: Downloading and keeping the evidence

Each page is downloaded, its type is checked (for example, is this really a PDF, or an HTML login page pretending to be one?), and the original bytes are stored as evidence.

Evidence is stored compactly. Web pages carry the same fonts and style sheets on every page of a site, and a page photographed in several states repeats them each time, so the engine keeps each of those large repeated pieces once and compresses the rest. Nothing is lost: every original reads back exactly as it was captured, and the evidence-integrity check still proves it. On the live John Hancock run this cut evidence from about 1.2 GB to 65 MB (about 18 times smaller), so a 5,000-page run fits comfortably within the 20 GB storage budget.

### Running again: only new or changed pages are scraped

The engine remembers every page it has captured, in every run, for each client account: each capture, the day its content was read, and the date the website itself gives for the page's last update. Every run checks this memory automatically, whatever list of addresses you give it (some seen before, some new) and whichever earlier runs they came from:

- **Never seen before**: scraped in full ("New page").
- **Seen before**: the engine asks the website politely whether the page changed since last time (a light request that the server can answer with "not modified"). If the server says it did not change, or the file is identical, or its visible text is word for word the same, the earlier content is kept and the slow browser step is skipped ("Reused (unchanged since <date>)").
- **The text decides, not the date**: a page whose wording changed is scraped again even when the website did not update its date (sites often edit pages without changing it), and a page whose date moved but whose text is identical is reused.
- **Old memories are not trusted forever**: content read more than 30 days ago (changeable per run, or "only reuse what was read after a given date", for example after an improvement to the engine) is compared but read again in full, so improvements to the engine reach every page. A page reused run after run still ages from the day it was actually read.
- **A bad day never erases what is known**: a failed or blocked visit is recorded but does not replace the last good capture, and a capture whose stored evidence is missing is not reused.

The site's update date is taken from what the page itself states (its structured data or tags). A web server's own "last modified" time is used only when it is at least a week old: many sites send the time the page was last rebuilt on their servers, not the time someone edited it. When a site does not say, the column is left empty rather than guessed.

After each run the memory is updated, so the next run starts from the newest captures. Before a run you can check a list of addresses: for each one, whether it was scraped before, when, and the site's last-update date. Older runs can be added to the memory with one command, every capture of one address can be listed, and the memory can be switched off for a run that should scrape everything afresh. It is kept in one small database file per client account; the earlier file format is imported into it automatically the first time and the old files are kept. On the John Hancock runs it holds 357 captures of 308 pages in under 5 MB.

### Step 5: Reading the page's text

The engine reads the page's HTML and splits it into blocks (headings, paragraphs, tables), and labels each block by layer: visible content, hidden text, navigation and footer, third-party widgets such as chat boxes, icon codes, and so on. Hidden and decorative text is kept but never mixed into the main content.

Site tools that are not content (sign-in menus, country pickers, search boxes, chat bubbles) are labelled as page furniture and left out of the main text. Words that sit side by side on screen as separate boxes (menu links, buttons, labels) are kept as separate words, never glued together. A wide table inside a box you scroll sideways is read in full: a column you reach by scrolling is on the page, not hidden.

Crossed-out text is kept and marked. A sale price shown as a struck-out "$200" next to "$150" is recorded as "Price ~~$200~~ $150", so a reader can see which price is current; the contradiction report never treats the crossed-out price as something the page still claims.

### Step 6: Opening the page in a real browser and exploring it

Many modern pages only show their content after the browser runs them, and much content sits behind tabs, accordions, "Load more" buttons, tooltips, pop-up dialogs, carousels and infinite scrolling. The engine opens each page in a real browser and explores it within strict rules:

- it only performs harmless, read-only actions (clicking tabs, expanding sections, scrolling, hovering);
- it never submits forms, logs in, buys, signs up or follows links away from the page;
- cookie banners are answered with "reject non-essential", never "accept";
- background tracking and "bot sensor" messages are blocked.

Shop pages hide content behind choices. Two extra explorations can be switched on for a job; they are off unless the job asks for them, so other jobs behave exactly as before:

- **Product options** ("select_variant"): on a product page, each option of a colour, size or similar picker (a drop-down list, buttons or swatches) is chosen on its own and the page is captured again, because the price, stock, product code and pictures often change. Each capture is labelled with the choice, for example "Colour: Blue". Options are tried one picker at a time and the picker is put back before the next, so the engine never tries every combination; at most 25 options per page by default. Options the shop shows as unavailable are not chosen. "Add to cart", "Buy now", wishlist and similar buttons are never pressed.
- **Listing filters** ("apply_facet"): on a category or listing page, the filter groups are read (for example Brand, Colour, Price), with each option's name, the number of products shown next to it and, if it is a link, its address. Filters that are links lead to other pages, so they are listed as addresses for the job's rules to decide on, never opened from this page. Filters that change the list in place (tick boxes, toggle buttons) are applied one at a time, captured, and taken off again; at most 25 per page by default. "Apply", "Clear all" and other form buttons are never pressed.

Choices beyond the limit are reported as gaps, so the page shows as "partly captured" rather than complete. To switch them on, add "select_variant" and/or "apply_facet" to the job's list of allowed interactions; the limits can be changed in the same place.

The engine then compares what the browser shows with what the raw HTML contained, so content that appears only after loading, or disappears, is noticed.

If the browser cannot open a page (for example it does not load within 30 seconds because the network or the website is slow), the engine tries again by itself: after 30 seconds, then 90 seconds, then 3 minutes, four tries in all. A slow spell usually lasts minutes, so tries a few seconds apart would all fail together. A page that still cannot be opened keeps the text read from its HTML and is marked "partly captured", never dropped.

### Step 7: Documents and images

Linked PDFs, Word, Excel and PowerPoint files, and images are downloaded too (one link deep). PDFs are read including tables that continue across pages, form fields, comments and hidden layers. Scanned pages and images are read with OCR (optical character recognition: software that reads text from pictures). The OCR engine is Tesseract, which runs on the local machine for free, with 25 core languages (including English, Hindi, Gujarati, Tamil, Arabic, Chinese, Japanese and the main European languages).

### Step 8: Checking (the "verification brain")

Before giving a verdict, the engine runs a set of checks on each page. Each check writes a gap when something is wrong:

| Check | In plain words |
|---|---|
| Inventory (R1) | Was everything discovered on the page accounted for? |
| Access (R2) | Was the page (and its parts) actually obtained, or blocked or failed? |
| Frames (R3) | Were embedded frames inside the page also read? |
| Visible-text coverage | Is every line of text the browser shows on the page also in what was extracted? If not, the page is marked "partly captured" and the missing lines are quoted. |
| Visual text (R6) | Does every line of text visible on the screenshot appear in what was extracted? Cookie banners and chat bubbles that appear on the screenshot are read at the same moment and recognised as widget text, so they do not cause false alarms. |
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

At the end of a run the engine writes a bundle of files. Every file name starts with the run's name (for example `jh2_r1_url_content.xlsx`), so files from different runs never get mixed up once downloaded:

| File | What you use it for |
|---|---|
| Project report (Excel) | The one workbook most people need. Sheet "Pages": every address with its whole text (line breaks shown), whether it was fully extracted (Yes/No), when its content was last scraped, when the site says it last changed, and what this run did with it ("New page", "Reused (unchanged: server said not modified)", "Re-scraped (updated: visible text changed)"), a plain "Extraction completeness" statement ("Complete", "Incomplete: 1 tab not opened (Pricing)", "Complete, but the page kept changing", "Placeholder page (Lorem ipsum)"), an overall Confidence of High, Medium or Low that can only be High when the page is fully captured, the share of the captured text confirmed against the live page, how reliably it was read (text from the page's code is 100%, text read from images lower), and what is missing. Sheet "Matching content": every pair of addresses that carry the same text, listed once (A with B is never repeated as B with A), by exact word-for-word matching (menus, headers and footers left out), with two percentages: how much of the first page's text is on the second, and how much of the second's is on the first. They differ when the pages differ in length: a short page can be 100% inside a long page while the long page is only 20% inside the short one. To see every match of one page, look for it in both address columns. Each pair also shows the shared text itself, every shared passage in full (separated by "[…]"), how many words they share and how many separate passages there are; until 29 October only a short example of up to two cut passages was shown, so a 100% match looked incomplete. Nothing is shortened. Because Excel shows only about 27 lines of one cell on screen, a sheet "Shared text by line" also lists every line of every shared passage on its own row, numbered by pair, passage and line, so the whole match can be read without opening cells. Very long page text that does not fit in one Excel cell continues in extra "Content_part_2", "Content_part_3" columns; "Content_parts" says how many cells the text uses and "Content_split" says whether it was split (normally 1 and FALSE). Sheet "How to read" explains every column. |
| R1 URL content (Excel, CSV) | One row per address: verdict, word count, gaps, and the whole extracted text. Long text is split across columns so Excel never cuts it off. |
| Review queue (Excel, CSV) | The pages that need a human decision, most serious first, with a blank column for the reviewer. |
| R2 overlap (Excel, CSV) | Groups of pages that share the same text (duplicate or near-duplicate content), with the shared passages quoted. |
| R4 contradictions (Excel, CSV) | Pairs of statements across the site that may contradict each other (for example two different prices for the same plan), with both quotes and a reviewer column. Pairs whose context differs (different years, countries or products) are listed separately, not as contradictions. |
| URL Content Explorer (web page) | An offline page to browse every address, its verdict, its gaps and its content. |

### The analyst web app

Besides the files, there is a web app for reviewing runs in a browser on your own computer. It never changes a run's results; the one thing you can save in it is your own decisions. It shows all runs; an overview of each run with charts of verdicts, the most common reasons pages were not verified and what to do about them; a searchable, filterable table of pages; a detail view per page (its content, issues, facts and technical details); duplicate content; possible contradictions; the pages that need your review; downloads of every report; and a dark mode.

Reviewers can record decisions directly in the app: for each possible contradiction (confirmed, not a contradiction, different context) and for each page that needs review (for example "get the content by hand" or "leave out of the results", with a note). You enter your name once; every decision is saved with your name and the time, and every earlier answer is kept, so there is a full history. The next report for that run starts with these decisions already filled in, in the Excel files too.

### Step 11: Getting pages ready for the knowledge base ("stamping")

The next stage of the project turns a finished run into a knowledge base: every service and product a website offers, with all of its content mapped to them, so websites can be searched, compared with competitors and checked for gaps. Step 1 of that work is done: one command ("stamp") reads a finished run and its stored evidence, without visiting the website again and without any AI model, and writes three files.

- **A stamp per page**: its title, description, canonical address, language versions, social-media tags, the page's main heading, its breadcrumb trail, where it sits in the site's address structure, the services, products, prices, questions and dates it declares in its structured data, the calls to action in its content (for example "Get a quote"), the numbers it states, word counts, a readability score for English pages, a one-sentence summary, and its verdict and dates from the run.
- **The site's menus, once per run**: the full menu tree as visitors see it, including mega-menus that only appear when a visitor opens them (the engine merges every opened menu into one tree). On John Hancock this gives the whole product hierarchy, for example Life insurance › Products & services › Term life insurance. This is the starting point for the list of offerings in the next step.
- **Sections of each page ("chunks")**: each page's main content split into sections of a few hundred words, ready for search.

**Which pages go in.** Images, tracking pixels, blocked pages, "page not found" pages, error pages, placeholder pages ("Lorem ipsum") and exact copies of another page are left out, each with the reason written on its stamp. Pages that are only nearly the same are kept, because they may differ in the details that matter (for example two fund notices that differ only in their numbers); the near-copies are listed on each page's stamp.

**Splitting pages without losing their meaning.** Cutting a page into sections can lose context, so every section keeps what it needs:

- a header naming the website, the page and the headings above it, plus the page's one-line summary, so a section never loses what it is about;
- links to the sections before and after it, and the last sentence of the previous section when a long passage had to be split;
- tables, question-and-answer panels and forms are never cut in half;
- a long passage is split between sentences, never inside one; very short sections are joined to their neighbours;
- the section text itself is kept exactly as extracted, with links back to the evidence, so every answer can be cited.

On the John Hancock run (39 addresses) it took about 9 seconds: 12 pages went into the knowledge base (20 exact copies, 4 images, 1 tracking pixel, 1 error page and 1 placeholder page left out), giving 51 sections of about 155 words on average (none over about 400), a header menu tree of 72 entries and a footer of 58.

### Step 12: The offering catalog ("website theming")

A second command ("catalog") turns the stamps into the website's catalog of offerings: every service, product and programme it offers, arranged as a tree of families, offerings and sub-offerings, with a card for each. It writes a file for other tools and an Excel workbook (`<run>_offering_catalog.xlsx`).

**How the list is found.** The engine reads the website's own menus, including the mega-menus, and sorts every entry: an offering group ("Products & services") holds offerings; tasks ("File a claim", "Sign in"), tools ("calculators"), content ("Insights"), corporate pages ("Careers", privacy links) and audiences ("Individuals", "Financial professionals") are set aside and listed separately. Entries that point to the same page, or that have the same name once words such as "insurance" are set aside, become one offering with both names kept. Products and services the pages declare in their structured data are added, and so are folders of the website shared by several pages.

**Every page is placed.** Each page is put under the offering whose web address it sits in (or that its breadcrumb or structured data names); pages that belong to no offering are filed under the section that covers them (for example News); the rest are listed as unplaced.

**Every value has its proof.** A card holds the offering's description, audiences, numbers (prices, ages, periods), questions, calls to action, tools and pages. Each value names where it came from: the menu entry, the page, the structured-data field, or the exact section and sentence. Nothing without proof is written. Each offering also shows how many independent places list it, so a reviewer can see which rest on a single menu.

**Optional local AI, never trusted blindly.** When the company's own AI server (Ollama) is connected, two extra steps can run; only servers on this computer or the company network are accepted, so client content never leaves the company and nothing is paid:

- Similar names are compared by meaning; two offerings are merged only when they are also in the same family and share words. Otherwise the pair is listed as a suggestion for a reviewer.
- The AI reads each offering's sections and proposes features, deliverables, audiences and pricing. Each proposal must quote the section word for word; the engine checks the quote and drops anything that does not match. A proposed kind of offering (product, service, ...) is applied only when the page's structured data agrees; otherwise it is a suggestion.

On the live John Hancock run (303 addresses), without AI, in a few seconds: 5 families (Life insurance with Permanent, Hybrid, Term, Executive benefits and Vitality; Long-term care; Annuities with Individual and Group annuities; Investments; Retirement with IRAs) and 7 more offerings listed only in the footer (for example College savings, Rollover, Travel insurance), with 63 menu entries set aside as tasks, tools, content and corporate pages. 271 of the 272 pages were placed: 4 under Individual annuities, 267 under News and the podcast sections (this run's addresses were mostly news releases); 1 privacy PDF is unplaced.

### Step 13: The SEO and GEO inventory

A third command ("inventory") lists everything on each page that matters to search engines and to AI answer engines (ChatGPT, Perplexity, Google's AI answers), not only the text a visitor reads. It re-reads the pages the run already saved, so it visits no website and costs nothing, and writes a file for other tools and an Excel workbook (`<run>_seo_inventory.xlsx`).

- **Every link**: where it points, the words it is shown with, whether it goes to the same website, another part of it, another website, a download, an email address or a phone number, its "do not follow" and "sponsored" markings, the part of the page it sits in (menu, main content, card, footer) and the heading above it.
- **Every image**: its description for blind visitors and for search engines (the "alt text"), flagged when missing, empty, or just a file name; its caption, size, whether it loads late, and where it links to.
- **Every video, audio clip and embedded frame** (YouTube, maps, forms, chat widgets), who provides it, and whether a video has captions.
- **The page's labels**: title and description with their length, the "official address" (canonical), instructions to search engines (for example "do not index"), language and language versions, link-preview tags for social media, and the outline of headings.
- **Structured data** (the machine-readable facts some pages carry, for example a product's price) is listed in full and checked against the fields Google requires before it shows a rich result.

**Where each item was found.** Every link, image and video says whether it is in the page the server sends ("HTML and browser"), only appears once the page's scripts have run ("Browser only (needs scripts)"), or only appears after a click on a tab, menu or "show more" ("After a click only"). This matters because many AI crawlers do not run scripts: anything that needs them is invisible to those crawlers.

**Issues, in plain words.** Each page gets a list of problems, graded error, warning or notice, with examples: titles too long, missing descriptions, several main headings, skipped heading levels, links with no words or with "read more", links marked "do not follow" to the site's own pages, images without descriptions, videos without captions, structured data that cannot be read or lacks required fields. An "Issue summary" sheet counts how many pages each problem affects.

On the John Hancock run (281 pages read, about a minute, $0): 15,813 links, 2,139 images and 311 videos and frames. Nearly every link on the site (15,636) exists only after scripts run, because the menus and links are drawn by the page's scripts rather than sent by the server, and 11,722 appear only once a menu or tab is opened. 262 pages have titles longer than search results show, 230 have no description, 242 have more than one main heading, and 1,033 images are marked as decoration, so crawlers learn nothing from them.

### Step 14: Page outlines and page types

A fourth command ("structure") rewrites every page as a clean outline in the order a visitor sees it, and says what kind of page it is. It writes one Markdown file per page (readable by people and by AI tools such as Claude or ChatGPT), a file for other tools, and an Excel workbook (`<run>_page_types.xlsx`).

**The outline.** Each page starts with its title, summary, address, publication and update dates, author, page type and breadcrumb trail, then its content: sections by heading, paragraphs (with their links), lists, tables, images with their descriptions and captions, videos, forms and buttons. The page's building blocks keep their shape: tabs are listed with their labels ("Tab: Pricing"), accordions and questions-and-answers with each question and its answer, groups of cards with each card's title, and carousels. Header, footer, menus, cookie and chat boxes and video-player controls are left out; the site's menus are kept once for the whole site. Text inside a closed tab or accordion is kept and marked "shown after a click"; text that only appeared once the engine clicked something is placed under the button that revealed it and marked "appears after a click". When a page is built from web components, the outline is read from the engine's composed view, which includes their contents. Each piece of text is matched to the engine's extracted text, so it can still be traced to the stored evidence.

**The page type.** Code decides whether a page is a product, a category (a grid of products), a service, an article, a case study, questions and answers, contact, about, careers, legal, pricing, a location, an event, a form, the home page, a document or other. It weighs independent signals: the page's structured data, its address ("/products/", "/blog/", "/case-studies/"), and what it shows (an add-to-cart button, prices, size or colour choices, a grid of priced items, filters, an author and date, a form). The reasons are listed in plain words with a confidence; a page with no clear signal is "other" rather than a guess. From all pages the engine also names the kind of website: e-commerce, B2B services, publisher or mixed.

On the John Hancock run (303 pages, about a minute, $0): 7,455 paragraphs, 551 sections, 756 cards, 255 accordion items in 33 question-and-answer blocks, 244 tables and 760 images; 89% of the outline's text is matched to extracted text; 268 pages are articles (news releases) and 33 are help-centre question pages, so this run reads as a publisher site.

## 5. Safety and cost promises

- Read-only: the engine never submits forms, logs in, buys, unsubscribes or clicks anything that changes the website.
- Polite: robots rules and requested delays are respected; a struggling site is paused, not hammered.
- Free by default: every run starts at $0. Paid access (Bright Data) needs your explicit approval per website, with caps of $5 per run and $25 per day, a monthly cap and a kill switch.
- How paid access works: when a website blocks every free method, the engine stops and asks. An operator can approve that one website, for a set amount (for example $5 per run), with one command; nothing else changes. Before every paid request the engine checks the kill switch, the approval and every limit (per run, per day, per month, per website and per page), and refuses rather than overspend. Every paid request is written to a spending log, and one command shows what has been spent and which websites are approved. Prices are counted on the high side, so the log never understates the bill.
- Private networks are off-limits: the engine checks every address before contacting it, at every redirect. The basic downloader also connects only to the exact public address it checked, so a trick that swaps the address mid-way cannot redirect it into a private network (the browser and the Chrome-like downloader re-check each request instead).
- Evidence first: originals are stored before anything is changed, so every claim can be proven.

## 6. Results so far

| Test | Result |
|---|---|
| Live John Hancock website, 303 pages (26 October 2026) | 279 of 303 VERIFIED_WITHIN_SCOPE at $0, run in 13 batches with unfinished pages re-run automatically. Of the rest: 15 earnings pages missed the last column of a sideways-scrolling table (found by the new coverage check, fixed the same day), 3 pages did not open in the browser during a slow spell (now retried automatically over several minutes), 4 were flagged because cookie-banner and chat-bubble text read from the screenshot was not part of the page's content, and 2 are placeholder pages. |
| Live John Hancock website, 34 pages (21 October 2026) | 32 of 34 pages VERIFIED_WITHIN_SCOPE at $0 with every check switched on (stricter than on 19 October: text reading, missing-text and leak checks). The other 2 are correctly flagged NEEDS_REVIEW: their live content is "Lorem ipsum" placeholder text. Reached by opening FAQ sections built as web components, matching text the way the browser shows it, and not letting a brief network hiccup pause the whole site. |
| Live John Hancock website, 34 pages (19 October 2026) | 32 of 34 pages VERIFIED_WITHIN_SCOPE at $0 with the checks available that day. The other 2 correctly flagged NEEDS_REVIEW because the live pages contain "Lorem ipsum" placeholder text. Before the browser-exploration work, all 34 were PARTIAL. |
| Same website, re-run with the stricter checks and OCR (20 October 2026) | 3 of 34 VERIFIED. This is the more honest number: the new channel-agreement check found text that exists in the page code but was not captured from the browser on 21 pages (frequently-asked-question answers on the form pages, half of one press release), so those pages were less complete than the earlier run suggested. On 8 pages a text-joining fault introduced on 20 October glues words together; it is being fixed. 2 pages still contain placeholder text. |
| Adversarial test suite (28 trick pages) | Zero pages wrongly marked VERIFIED. Each trick (fake PDFs, challenge pages, form walls, paywalls, disguised fonts, soft redirects) gets the expected verdict. |
| Automated tests | 771 tests passing, 93.7% of the code exercised by tests. A CI pipeline runs all quality checks and the release gate on every change once the code is on GitHub. |
| Contradiction report on John Hancock | After the 20 October fix: 0 false contradictions (previously 17); differing-context pairs listed separately. |

## 7. What it does not do yet

- Paid access through Bright Data's remote browser is not built; pages fetched through paid access are read from their page code, not opened in a browser through the paid connection.
- Durable database: the PostgreSQL storage exists but has never been tested against a real database.
- Starting runs from the web: the analyst web app reviews finished runs, but runs are still started from the command line, and there is no live progress feed or deployment package yet.
- Some content types: text drawn on canvas or in CSS, combinations of product options (each option is captured on its own, never every combination), audio and video, archives and email files, and old-style .doc and .xls files.
- Semantic similarity: finding pages that say the same thing in different words (planned with a vector database).
- The rest of the knowledge base: labelling every page and section by its content (not only by its address), search across sections, and competitor comparison and gap reports are designed but not built yet (steps 1 and 2, stamping and the offering catalog, are done). The AI steps of the catalog have been tested against a simulated server only, until the company's Ollama server address is available.
- Hidden page text: some text present in the page code (for example frequently-asked-question answers) is not yet revealed and captured by the browser exploration; the engine flags these pages for review rather than counting them complete.
- Skipping a page without asking the website at all (for example from the dates in the site's sitemap): every page seen before still gets one light request.

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
| URL history | The engine's memory of every page captured in every run, used to reuse unchanged pages. |
| Stamp | The set of facts recorded about one page before it goes into the knowledge base. |
| Chunk (section) | A piece of a page's content, a few hundred words long, that keeps a note of which page and heading it came from. |
| Offering | A service, product or practice a website offers; the knowledge base maps every page to them. |
| Offering catalog | The tree of a website's offerings (families, offerings, sub-offerings), each with a card of evidenced facts. |
| SEO / GEO | Search engine optimisation / generative engine optimisation: making pages easy for search engines and for AI answer engines to find, read and cite. |
| Alt text | The written description of an image, read by screen readers, search engines and AI tools. |
| Structured data | Machine-readable facts in a page's code (schema.org), for example a product's name and price. |
| Page outline | A page rewritten in reading order with its sections, tabs, accordions, cards and tables, as Markdown. |
| Page type | What kind of page it is (product, category, service, article...), decided by code from several signals. |

## 9. Change log

| Date | Significant change |
|---|---|
| 2026-10-29 | Project report, "Matching content": the shared text of each pair of pages is now shown in full (every shared passage, with the number of passages), instead of a short example cut after 300 characters, with the number of shared words and a "Shared text by line" sheet that shows every shared line on its own row. |
| 2026-10-28 | Page outlines and page types: a "structure" command rewrites every page as an outline in reading order (sections, tabs, accordions, questions and answers, cards, tables, images, videos, forms), one Markdown file per page, with text behind clicks marked, and labels each page as product, category, service, article and so on, and the site as e-commerce, B2B services, publisher or mixed. John Hancock: 303 pages, about a minute, $0. |
| 2026-10-28 | Browser exploration can now, when a job asks for it, choose each product option (colour, size and similar) and capture the page for each, and read a listing page's filters, applying in-page filters one at a time. Both are off by default; cart, buy, form and sign-in buttons are never pressed. |
| 2026-10-27 | A page whose download meets a bot check is now tried in the real browser before being reported blocked; if the browser gets through, the page is captured normally. |
| 2026-10-28 | SEO and GEO inventory: an "inventory" command lists every link, image, video and embedded frame of each page, its title, description, canonical and search-engine instructions, and its structured data checked against Google's required fields; each item says whether it needs scripts or a click to appear, and each page gets graded issues. John Hancock: 281 pages, about a minute, $0. |
| 2026-10-27 | URL history across runs: any list of addresses is checked against every earlier run; pages that did not change (the server says so, or the file or its visible text is identical) keep their earlier content and are not scraped again, changed and new pages are scraped in full, and content older than 30 days is read again. Before a run, a list of addresses can be checked (scraped before? when?). The project report gains "Last scraped", "Site last modified" and "Scrape action" columns. |
| 2026-10-27 | Knowledge base step 2: a "catalog" command builds each website's offering catalog from its menus, structured data and folders, places every page under an offering or section, and fills evidence-backed cards; optional local AI adds checked features and suggestions. John Hancock (303 addresses): 5 families, 15 more offerings, 271 of 272 pages placed, at $0. Menu links that are script buttons are no longer read as page addresses. |
| 2026-10-27 | Knowledge base step 1: a "stamp" command records each page's facts (titles, descriptions, structured data, menus, calls to action, numbers, dates), builds the site's full menu tree including mega-menus, leaves out images, error, placeholder and duplicate pages with the reason, and splits each page into sections that keep their page, heading and neighbours. John Hancock: 12 pages, 51 sections, a 72-entry menu tree, in about 9 seconds at $0. |
| 2026-10-26 | Matching-content sheet columns renamed for clarity: "URL (A)", "Matching URL (B)", "% of A in B" and "% of B in A". Chat-widget invitations (for example "Live chat") are no longer treated as missed page content. Unattended runs wait up to six minutes for held work, so render retries (30, 90 and 180 seconds) finish before a run stops. |
| 2026-10-26 | Columns of wide tables that you scroll sideways are captured (they were wrongly treated as hidden). A page the browser cannot open is retried by itself over about five minutes instead of a few seconds. Live John Hancock, 303 pages: 279 verified. |
| 2026-10-26 | Project report: each pair of matching pages is listed once, with the shared share in both directions. |
| 2026-10-24 | New check that every line of text the browser shows was extracted. Web components no longer mistaken for hidden text; a duplicate copy of text never replaces the visible one; site tools (sign-in, country picker, chat) kept out of the content; words in side-by-side boxes no longer glued; a page is judged settled by its text, not by background elements. |
| 2026-10-22 | Paid access (Bright Data) built but off by default: one approval per website with a spending cap, checked before every paid request, with a spending log. |
| 2026-10-21 | Reviewers save decisions in the web app (with name, time and full history); the next report carries them forward. |
| 2026-10-21 | Live John Hancock: 32 of 34 verified again with all checks on (the 2 others are placeholder pages). FAQ sections built as web components are now opened, text is matched the way the browser shows it, and a brief network hiccup no longer pauses a whole website. |
| 2026-10-22 | Project report made unambiguous: an extraction-completeness statement and a High/Medium/Low confidence per page that always agree with the page's status; the confirmed share is named for what it measures; page text keeps its line breaks in Excel. |
| 2026-10-21 | Project report: one Excel workbook per run with every page's text, whether it was fully extracted, the confirmed share and the confidence, plus, for every page, which other pages carry the same text and how much (exact matching). |
| 2026-10-21 | Evidence stored compactly: repeated fonts and style sheets kept once, the rest compressed, originals unchanged. About 18 times less disk on the live John Hancock run; a 5,000-page run now fits the 20 GB budget. Older evidence can be converted with one command. |
| 2026-10-21 | Crossed-out text (for example an old sale price) is kept and marked with ~~ instead of reading as a current price. |
| 2026-10-20 | Report file names start with the run's name, so downloads from different runs stay separate. |
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
