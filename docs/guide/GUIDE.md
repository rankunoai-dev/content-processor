# Content Processor: A Plain-Language Guide

Last updated: 10 November 2026 · Code version: main after the November audit: false alarms removed from verdicts, a live batch view, confidential projects, a sleep-proof scrape clock, and a deployment recipe

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

**What is and is not a block (corrected after the November audit).** A missing page (HTTP 404 or 410) is never kept as if it were content, whatever its title says; it is marked "looks like a page not found", gets no browser pass and does not count against the website's health. A page is only *suspected* of being a hidden "not found" page when its title contains the whole words "404" or "not found" and its text says so too ("Trial and error" no longer triggers it), and a suspect keeps its text. A sign-in form in a site's header no longer makes every page a login wall: a page is locked only when the server says so (401), redirects to a login address, or is nothing but a password form. Code embedded in a page is ignored when judging how much readable text it has, so a phrase inside a script cannot make a normal page look like a CAPTCHA. A security cookie from Akamai or similar on an error answer is reported as the error it is, not as a bot challenge. A page whose server copy is almost empty but loads a CAPTCHA script (app-style sites) is handed to the real browser first and is called a CAPTCHA wall only if the browser also finds nothing but the check.

**One blocked page no longer moves the whole site.** A blocked page is retried one access tier up on its own; the whole website moves up a tier only after three different pages are blocked at the same tier in the same scrape. Paid tiers follow the same rule: an approved website buys only the pages that were blocked, not every page. The pause-on-repeated-failure safety is unchanged.

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

A CAPTCHA box on a form (a contact or newsletter form protected by reCAPTCHA, hCaptcha or Cloudflare Turnstile) is not a block: many websites carry one on every page. A page with a CAPTCHA counts as blocked only when the website answered with an error, the page asks the visitor to prove they are human, or the page has almost nothing on it besides the CAPTCHA. Before 9 November 2026 any page carrying such a box was reported as blocked, which stopped a 7,273-page GEP scrape at its first pages.

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

**A site's menu is explored only on the first pages.** The header menu is the same on every page, so clicking through it again on each of thousands of pages only wastes time, and the items that cannot be clicked used to make every page look "partly captured". Now, during a project scrape, the engine remembers what each control did. When a control has been seen on two different pages with the same label, the same surrounding text and the same result, later pages leave it alone and list it as "site-wide control". A control is explored on every page if its text differs from page to page (an answer written for that page) or if clicking it gave different results on different pages (a panel filled in after the click). "Add comment" buttons are treated as site tools, not content. On GEP's real pages this took a page in the browser from about 36 seconds to about 20, with the same article text word for word. The first two pages of each scrape are still explored in full, so they can show as "partly captured" because of the menu.

The engine then compares what the browser shows with what the raw HTML contained, so content that appears only after loading, or disappears, is noticed.

If the browser cannot open a page (for example it does not load within 30 seconds because the network or the website is slow), the engine tries again by itself: after 30 seconds, then 90 seconds, then 3 minutes, four tries in all. A slow spell usually lasts minutes, so tries a few seconds apart would all fail together. A page that still cannot be opened keeps the text read from its HTML and is marked "partly captured", never dropped.

**What is not counted as missing content.** Three things used to make almost every page of a real site "partly captured" although the article itself was complete, and no longer do: the invisible frames that tag managers and advertising pixels add to every page (hidden frames, and frames whose address is a known tag manager or pixel, are never content); the choices inside a drop-down list, which a browser lists one per line but which are form furniture, not text the page says (a country picker alone is 200 lines); and SVG logos, which are drawings written as text files, not pictures, so they are read as text and never sent to the picture reader. On GEP's live scrape the tag-manager frame alone had marked 466 of 466 pages "partly captured".

### Step 7: Documents and images

**Images are downloaded once, by the browser.** A page's images used to be downloaded one by one, a second apart, and then loaded a second time by the browser that renders the page: on GEP that was 120 to 180 extra requests per batch of 25 pages, about 3 to 4 minutes, all spent waiting between requests. Now, when a scrape renders pages, the images ride along with the page's browser visit: every image the browser downloads to show the page is kept from that one download, with the same evidence, the same link to its page, the same duplicate check and the same reading step a separate download produced. The rules still apply to each image (robots rules, allowed file types, "is this really an image?"), and any image the browser did not load, or whose answer was cut short, or whose page failed or was blocked, is downloaded afterwards exactly as before, so nothing is lost. The page itself is still downloaded first: that is what gives the raw-page-versus-rendered comparison, the cheap "has it changed?" check on repeat scrapes and the access ladder. The only visible difference in the records is that an image is marked as captured "by the browser" rather than by the plain downloader. On the local test site the records before and after are identical apart from that mark, with two fewer requests.

**Documents and images no longer wait for the browser.** While the engine is still fetching and rendering pages, it reads the PDFs and images it has already downloaded at the same time. Reading a file sends nothing to the website, so the one-request-per-second courtesy to each site is exactly as before; the run simply finishes sooner (the old order, fetch then render then documents, is still available with `--sequential-stages`). Files are read by a small number of helper programs that stay running (`CP_DOC_PARALLEL_TASKS`, four on a workstation, one or two on a small server) instead of a new program per file that took a second or more just to start; a helper that hangs is stopped and replaced and the file is marked partial, one that crashes is replaced and the file is tried again, and helpers are refreshed every fifty files so they never grow large. The text-recognition program is checked once per run instead of three times per picture. At the end of a batch the screenshots of all rendered pages are read several at a time for the "nothing visible was missed" check, with the same result per page in the same order (about 97 seconds per batch of 25 GEP pages before; about a quarter of that now). The engine also waits exactly as long as courtesy requires between two requests to one site instead of checking the clock every half second (requests are still never closer than one second apart), skips a page snapshot it used to take and throw away, and no longer collects an accessibility description nothing read. Before and after these changes the engine produced byte-for-byte the same records on the local test sites.

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

The engine also recognises "gates" on a page: lead-generation forms in front of a report, age or professional-only gates, login prompts, delivery pincode pop-ups, "product unavailable" notices and subscription paywalls. Two of these raised false alarms on real sites and were tightened: an audience or age gate is reported only when the question sits in a pop-up with a button to confirm (a "for healthcare professionals" line in the footer is not a gate), and a lead-generation wall is reported only when the words about the asset and the instruction to fill in the form sit in the same part of the page as the form, and the page does not already publish a whole article of its own (300 or more words). A long article with a "download our research" form beside it is verified normally; on GEP that form had marked a third of the blog pages as walls. Sign-in menus and third-party widgets are recognised by whole words in their code names, so content sections named like "blog-inner" are no longer mistaken for site furniture.

### Step 9: The verdict

All gaps for a page are combined into one verdict using the strict order in section 3. A page is VERIFIED only when nothing stands in the way. The detailed status is always kept alongside, so a verdict never hides the reasons behind it.

**Re-scoring without a new scrape.** When a rule turns out to have raised a false alarm, the pages already scraped do not have to be fetched again: the command `reverdict` reads a run's stored records, removes the gaps the corrected rules would no longer raise (and re-runs the gate rules on each page's stored copy), and derives the verdict again. Nothing is ever added, a page is never upgraded past what its stored evidence supports, and the record says it was re-scored. A page that ran out of time ("budget exhausted") is never reused as a baseline for a later scrape.

### Step 10: The reports

At the end of a run the engine writes a bundle of files. Every file name starts with the run's name (for example `jh2_r1_url_content.xlsx`), so files from different runs never get mixed up once downloaded:

| File | What you use it for |
|---|---|
| Project report (Excel) | The one workbook most people need. Sheet "Pages": every address with its whole text (line breaks shown), whether it was fully extracted (Yes/No), when its content was last scraped, when the site says it last changed, and what this run did with it ("New page", "Reused (unchanged: server said not modified)", "Re-scraped (updated: visible text changed)"), a plain "Extraction completeness" statement ("Complete", "Incomplete: 1 tab not opened (Pricing)", "Complete, but the page kept changing", "Placeholder page (Lorem ipsum)"), an overall Confidence of High, Medium or Low that can only be High when the page is fully captured, the share of the captured text confirmed against the live page, how reliably it was read (text from the page's code is 100%, text read from images lower), and what is missing. Sheet "Matching content": every pair of addresses that carry the same text, listed once (A with B is never repeated as B with A), by exact word-for-word matching (menus, headers and footers left out), with two percentages: how much of the first page's text is on the second, and how much of the second's is on the first. They differ when the pages differ in length: a short page can be 100% inside a long page while the long page is only 20% inside the short one. To see every match of one page, look for it in both address columns. Each pair also shows the shared text itself, every shared passage in full (separated by "[…]"), how many words they share and how many separate passages there are; until 29 October only a short example of up to two cut passages was shown, so a 100% match looked incomplete. Nothing is shortened. Because Excel shows only about 27 lines of one cell on screen, a sheet "Shared text by line" also lists every line of every shared passage on its own row, numbered by pair, passage and line, so the whole match can be read without opening cells. Any sheet with more rows than Excel allows (about a million) continues on a second sheet with the same name and "(2)", so nothing is dropped. Very long page text that does not fit in one Excel cell continues in extra "Content_part_2", "Content_part_3" columns; "Content_parts" says how many cells the text uses and "Content_split" says whether it was split (normally 1 and FALSE). Sheet "How to read" explains every column. |
| R1 URL content (Excel, CSV) | One row per address: verdict, word count, gaps, and the whole extracted text. Long text is split across columns so Excel never cuts it off. |
| Review queue (Excel, CSV) | The pages that need a human decision, most serious first, with a blank column for the reviewer. |
| R2 overlap (Excel, CSV) | Groups of pages that share the same text (duplicate or near-duplicate content), with the shared passages quoted. |
| R4 contradictions (Excel, CSV) | Pairs of statements across the site that may contradict each other (for example two different prices for the same plan), with both quotes and a reviewer column. Pairs whose context differs (different years, countries or products) are listed separately, not as contradictions. |
| URL Content Explorer (web page) | An offline page to browse every address, its verdict, its gaps and its content. |

### The home screen: modules as tiles

The app opens on a **home screen** with one tile per module: Content Scraper, Knowledge search, Themes, Monitoring, Research, Gaps and Analyst cards. Each tile says what the module does and how many projects already have results in it. Clicking a tile opens that module: its list of projects, each with its state in that module (for example "9 themes · built 2 Nov" or "19 findings, 17 open, 2 high"), and a **New project** button. The same modules are listed in the left menu.

Projects are **shared**: a project (the client's websites and its competitors) is created once, from any module, and is used by every module, so themes, monitoring, search data, gaps and analyst cards all build on the same scrapes. Opening a project from a module opens it on that module's tab; the path at the top (for example Home / Gaps / JH test) leads back. "All runs" (scrapes run from the command line) moved to its own menu entry.

### The Content Scraper app (Module 1): projects

The engine is becoming one app with several modules. Module 1, the Content Scraper, lets anyone run a scrape from the browser, without the command line:

1. **Create a project**: a project name, your name, the client's name and a password (at least 8 characters). The password is stored only in scrambled form and cannot be read back. The form also asks **who the client competes with**: every module compares the client with its competitors, so if none is named it asks once more, and you can add them now or create the project without them (the Research tab suggests competitors later, see Step 19).
2. **Add the web addresses**: paste them (one per line) or import a file from your computer (.txt, .csv or Excel .xlsx; addresses are found in any column). Every address is checked: duplicates, addresses already in the list and unusable lines (for example ftp:// or text that is not an address) are counted and shown with the reason, never dropped silently.
3. **Start scraping**: one button. The addresses are worked in batches of 25, each in its own process so one crashed browser cannot stop the app; pages that did not finish are tried again, up to three times. The progress (pages done, batch, results so far) updates by itself, and a scrape can be cancelled. **A stopped scrape continues where it stopped.** Every download and every page opened in the browser is saved the moment it arrives. If a scrape is cancelled, fails, or the app or computer stops, press **Continue** next to it (a scrape caught by an app restart continues by itself). Finished pages are not repeated and what was already captured is not requested again, even in the batch that was cut off; the page is still read and checked by the current code and keeps the time it was really captured. Pages that ended blocked or failed get one more try when you press Continue, and the better result is kept. Errors and cut-off downloads are not saved, a saved response older than 7 days is requested again, and saved responses are removed when the scrape finishes. The same saved copies mean an image used across a whole site is downloaded once per scrape, not once per batch. Pages captured before and unchanged since are reused (see the URL history), so a repeat scrape is quicker. Free access only.

   **Watching a scrape as it runs.** The project's scraping tab shows what is happening inside the scrape, not only the overall count: a "Batches and pages" box lists every batch with its state (waiting, scraping, trying again, done), how many of its pages are finished, the colours of their results and which try it is on; for the batch being worked on it also says how many of its pages have already been downloaded and rendered. Clicking a batch lists its pages one by one: the stage each page has reached, its result, a "content captured" bar (the share of the page's main content verified as captured; menus and footers are not counted), the reason in plain words and the number of open issues. A "Live log" button opens the running batch's log as it is written, in plain English, with a pause button and a "problems only" filter. The same view is kept, folded, for earlier scrapes. Everything is read-only and refreshes by itself.

   **Sleep is not scrape time.** Each batch has two hours of working time. That clock used to keep running while the computer slept, so a laptop closed overnight woke up "out of time" and gave up on the rest of its batch; it now counts only time the scrape was actually working. Pages a batch did run out of time for are tried again on the batch's next attempt instead of being counted as done, and a page captured on a later try replaces the cut-off one. After the pages, the reports and the knowledge space are built in their own helper programs: those now stop when you cancel (the scrape then shows as cancelled and can be continued), stop by themselves after 30 minutes (reports) or an hour per step (knowledge), stop when the app is closed, and leave their last error lines in `reports.log` and `knowledge.log` in the run's folder. Two people pressing Start on the same website at once get one scrape, not two; wait estimates use the time a scrape actually ran; the batch counter never goes backwards when a scrape is continued; pages found along the way keep their best try; and deleting a project also removes its themes, monitoring checks, gap reports, research pulls and Claude Desktop ledger, and empties its knowledge spaces.

4. **Download the reports**: every scrape keeps its own reports. Each file is listed with what it is ("Start here: every page with its content…"), and all of them can be downloaded at once as a zip.

**Several websites per project.** A project holds the client's website and any number of competitor websites (name and address each), entered when the project is created or added later. Each website is kept completely apart: its own address list, scrapes, reports and searchable knowledge. Addresses of several websites can be added in one list: each goes to the website of its domain, and addresses of a website that is not in the project are listed so it can be added as a competitor. "Scrape all websites" scrapes each website on its own.

**The client's several websites (brands).** Many clients have more than one website: separate brands, regional sites, or several sites on one domain. A project can hold all of them as **the client's websites**, next to the competitors. The first is the client's main website; the others are entered under "The client's other websites or brands", or added later with "Add a website". The client's websites are always on the same side: the engine never compares two of them as competitors, whether it is grouping pages into themes, deciding who owns a theme, looking for gaps, searching, or preparing an analyst card, and this holds across all projects, even one that was entered the wrong way round. When one domain holds several websites, each gets its own subdomain or section (for example `brand.client.com` or `client.com/brand`), and every address goes to the most specific website.

**Notes and tags on every website.** Each website can carry a short note saying what it is (for example "B2B brand for hospitals, US only") and a few tags (such as "brand", "US", "B2B"). They are shown on the website's page, can be changed at any time with "Edit website", and are passed on to the theme map and the analyst so both know what each website is. A note describes the website; it is never treated as a fact found on its pages.

**One scrape at a time: the shared queue.** Anyone can create projects and add as many addresses as they like, but the app scrapes **one website at a time for everyone**, so one person's large website cannot slow everybody down or overload the machine. Every other scrape waits in one queue, in the order it was started. When you press Start, the app shows how many scrapes are ahead and roughly how long the wait is, and asks what should happen when your turn comes:

- **Start automatically**: it starts by itself as soon as the scrape before it ends.
- **Email me, and I start it**: you get an email when it is your turn and the queue moves on meanwhile; when you press Start (on the website's page) it runs next, before everyone waiting. If you do not start it within 72 hours it is cancelled and can be queued again.

Either way you can also ask for an email when the scrape finishes. The **Scrape queue** page (left menu) shows what is being scraped now, who is waiting in which order, each one's estimated wait (worked out from how long recent scrapes took per address), and whose turn has come. Email addresses are shown only partly (`g***@company.com`). Emails are sent through the organisation's mail server once it is set up (`CP_SMTP_*` settings); until then each message is saved on the server for the administrator.

**Project reports: duplication and contradictions across the client's websites.** Each website's scrape has its own reports. The **Project reports** tab makes one report for the whole project from the latest finished scrape of **each of the client's websites** (the main website and its brands). Nothing is scraped again, no AI model is used, and it costs nothing; about 30 seconds for 300 pages. Every build is kept and anyone can download them; building one needs the project password.

- **Contradictions by topic** (Excel, plus CSV and a file for tools): one row per topic that the website states in more than one way, in the columns *Topic, Version A, Version A URLs, Where A appears, Version B, Where B appears, Version B URLs, Severity, Recommended action, Approved answer*. The approved answer is left empty for the team. Example: "How long does it take to receive a death claim payment?" says *7–10 business days after the claim is in good order* on the help centre and the life-insurance FAQ, and *typically within 60 days of filing* on the death-claims page; severity High; agree one timeline on all three pages. The engine reads every sentence for times, amounts, percentages, ages, other figures, phone numbers and email addresses, and groups statements into one topic when they share enough uncommon words (the sentence plus the question or heading above it). "Within 60 days" and "60 days" count as the same; "business days" and "days" do not. A third or fourth version is listed under "Other versions". Each row ends with the block and evidence ids, so every snippet can be traced to the captured page.
- **Set apart, never hidden**: versions that split cleanly by state, audience, product or fund code, or website (brand) go to a "Differs by condition" sheet, since they may be intended. Figures from news and press-release pages, or tied to a date or period ("as of", "declared", different years), go to "Time-bound figures": they belong to a period and are not contradictions. On John Hancock this turned the 14,679 rows of the old pair-by-pair contradiction report (all monthly fund distributions in press releases) into 0 contradictions and 7 time-bound topics.
- **Severity by rule**: High for customer commitments (payout or processing times, fees, prices, eligibility, ages, coverage, limits, legal terms), Medium for product and contact details, Low for marketing figures (customers, awards, years in business).
- **Project report** (Excel): the same page-by-page report each scrape writes, now for all of the client's websites together: "Pages" (every page with its whole content, whether it was fully extracted, confidence, what is missing) and "Matching content" (every pair of pages sharing 10% or more of their text, how much of each, and the shared text in full), plus the shared text line by line. With one client website it is simply that scrape's own report.
- **Duplication** (Excel): pages that are near-copies (80%+ the same text) and groups of them, each with its website. The same text on two of the client's websites (say, the main site and a brand) is marked "across websites".
- **Building takes a while on large websites** (about 5 minutes for 300 pages of a client with several websites), so it runs in the background: the tab says it is building, you can leave and come back, and the report appears when it is done.
- **Competitors**, only when "Include competitors" is ticked: a separate workbook with each competitor's own contradictions and duplicate pages. A competitor saying something different is not a contradiction, so competitors never enter the client's contradiction sheets.

What it does not catch yet: one topic worded with completely different words, and yes/no statements ("is covered" against "is not covered"). Those need the planned Semantic module (search by meaning).

**Searching across websites.** After a scrape, each website's pages are prepared for search in their own store. The "Search all websites" tab finds the passages that answer a question and says which website (and which project's scrape) each came from; the best passage of each website can be shown side by side. By default it covers the client's website and **every website it competes with in any project**: websites are recognised as the same across projects by their domain, each is searched once in its most recent scrape, and a project that lists the client as someone else's competitor counts too.

**Who can do what:** anyone using the app can open a project and download its reports. Changing a project (adding or removing addresses, starting or cancelling a scrape, deleting it) needs the project password: entering it unlocks the project in that browser tab for 8 hours. Deleting asks for the password again. Wrong passwords are slowed down after five tries.

**Confidential projects.** A project can be marked "Confidential" when it is created or later in its Settings tab (after unlocking it). For a confidential project the text of its pages is never sent to Claude and never shown to Claude Desktop: Claude-run cards are refused for it (the context pack still works for a person and is headed "Confidential"), and Claude Desktop can only use the tools that return counts, names, web addresses, titles, themes, rankings and findings; anything that would show passages or changed text is refused with a plain reason. Downloads in the app are not affected: the people who run the project own the data.

**Running on a server.** The app no longer has to run on one computer. It can be told where to listen (`CP_BIND_HOST`, `CP_BIND_PORT`), which web addresses it may be reached by (`CP_ALLOWED_HOSTS`) and which pages may make changes in it (`CP_ALLOWED_ORIGINS`); without these settings it behaves exactly as before. A `Dockerfile` and `deploy/README.md` describe running the whole app in a container (Railway or similar): the memory to expect (about 1 GB per batch for ordinary pages, up to 2.5 GB for PDF-heavy ones with several readers), the folders that must live on a persistent volume, and the settings a small server needs (`CP_DOC_PARALLEL_TASKS=1`, the bundled browser). The server also stays small over time: it keeps only the four most recently used runs in memory (`CP_RUN_CACHE_ENTRIES`), builds project reports in a helper process with a 30-minute limit, and open pages ask about the queue less often. One caution the team must decide on before putting the app on the internet: anyone who can open the address can read every project and report without a password (only changes need one), so it needs a login or a private network in front of it.

If the app is closed during a scrape, the scrape continues where it stopped the next time the app starts; finished batches are not repeated.

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

### Step 15: The searchable knowledge base (search and questions with proof)

Four commands turn a run's page sections into a knowledge base you can search and question, with every answer traced back to the stored page.

**Checking what is ready ("kb-check").** It says whether the local AI model server (Ollama) is running with its models, whether the Postgres database with its vector add-on (pgvector) is reachable, and what the next steps will therefore use. Nothing is sent to an outside AI service, and the cost stays $0.

**Which model servers are accepted.**
- **Accepted by default:**
  - a server on this machine;
  - a server on the company's private network, by its address (for example 192.168.1.50) or by a name that points only to private addresses (for example ollama.office.lan).
- **The company's own server in the cloud** is accepted only when an operator switches that on (`CP_ALLOW_REMOTE_MODEL_ENDPOINT=true`) and the address uses https, so page text is never sent unencrypted. A plain http address on the internet is always refused, and the refusal says why.
- **A server behind a sign-in gateway** can be given its access key (`CP_MODEL_API_KEY`).

**Filling the knowledge base ("index").** Every section of every page is turned into a list of numbers that captures its meaning (a "vector"), using the free bge-m3 model on Ollama. Sections whose text has not changed since the last time keep their vectors, so a re-run only works on new or changed sections. Each section is stored with its exact text, its headings, the page it came from and the proof (evidence) behind it. The knowledge base lives in Postgres when that is set up, and otherwise in a single local file that needs nothing installed. If the AI model server is not running, the engine falls back, in this order:

1. **A small multilingual model on this computer's processor** (about 220 MB, downloaded once, then offline and free). It understands meaning: it finds the same question asked in other words or in another language. It is lighter than bge-m3 and reads only about the first 128 words of each section.
2. **A word-matching stand-in**, only when that small model cannot be used either.

The knowledge base remembers which one was used. When a better one becomes available (the small model over word matching, bge-m3 over both), the next filling rebuilds it by itself. Better vectors are never replaced by weaker ones. Search says, for each website, whether its results came from bge-m3, from the lighter model ("meaning from the lighter CPU fallback model"), or from exact words only.

**Labelling sections (only when two signals agree).** Each section is labelled with what it does (a claim, a feature, pricing, proof such as ratings or awards, a question and answer, a call to act, or a legal disclaimer) and with the product or service it is about. A label is accepted only when at least two independent signals agree: where the text sits on the page (its heading, an accordion, a form, the page's own buttons and structured data), what the words say (prices, questions, ratings, legal wording), how close its meaning is to each offering, and, when available, a local AI model. One signal alone is not enough; signals that disagree send the section to a person instead of guessing. The workbook `<run>_chunk_labels.xlsx` lists every section with its labels and the signals behind them, and a "Needs review" sheet.

**Searching ("search").** A search looks two ways at once: by meaning (sections that say the same thing in other words) and by exact words (form numbers, fund codes, product names), and puts first the sections both searches found. Each result shows its headings, its full text, the page address, and the evidence and block identifiers. A search can be limited to one product or service and everything under it.

**Asking a question ("ask").** The best sections, plus the section before and after each so nothing is read out of context, are given to a local AI model, which must answer in short statements, each with the exact words it relied on. The engine then checks every statement: the quoted words must appear word for word in the cited section, every number in the statement must be in the quote, and the statement may not say more than its source. Statements that fail are dropped and listed with the reason. Without an AI model, or when nothing passes the check, the answer is the best-matching sentences quoted exactly. Every point links to the live page with the quoted words highlighted, and names the evidence it came from.

On the John Hancock run with the small processor model, filling all 1,308 sections took 23 seconds. "Tips to rest better at night" then finds the "Better sleep with Dr. Matthew Walker" episode, where word matching found press releases. It is still far from bge-m3: "who gets my money when I die" does not reach the change-of-beneficiary form with either. Before, with the word-matching stand-in, and a re-run reused all 1,308 vectors. Searches and questions answer with exact quotes (for example, the states where the Oura Ring discount is not offered). Because this run is almost all news releases, few sections carry an accepted label from page code alone: 12 questions-and-answers and 4 sections about annuities. The rest are left unlabelled or marked as having only one signal rather than guessed. The AI model signal, once Ollama is running, is the second opinion that will confirm more of them.

### Step 16: Senior Analyst Action Cards (analyses written by Claude, checked by code)

An action card is a ready-made analysis a senior SEO analyst would run, for example "E-E-A-T & Trust Proof Audit", "Topic Cluster & Pillar Map", "Head-to-Head Competitor Gap Audit" or "Winning Content Brief". You choose a card and a project; the client's and competitors' websites fill in by themselves, and you can narrow it to a topic, a keyword or two pages. There are 15 cards across the modules: 12 run today on the scraped data, and 3 keyword cards wait for the Research module (Semrush).

**What a card does.** It gives Claude a detailed, step-by-step brief written for that analysis, and the data it needs: passages from the websites, page facts, the offering catalog, SEO issues, near-duplicate pages, and what changed between two scrapes. Claude may look up more, but only on the project's websites and its competitors. It writes the deliverable in a fixed shape: tables, scores, priorities, and a section saying what the analysis could not check.

**Every claim is checked.** Claude must cite, after every statement, a reference the engine handed out in this run. When it finishes, the engine checks that every reference is real, that every quote appears word for word on the page, and that statements with numbers are cited. It then adds a "Sources" table with the page address and the proof (evidence) behind each reference, so those details never come from the AI. A deliverable that fails a check is marked "needs review" with the reasons. Claude is told never to estimate search volume or traffic, and never to follow instructions written inside the websites it reads.

**In the app.** Each project has an **Analyst cards** tab. It shows the cards grouped by module (cards that need a module not built yet are greyed out, with the reason). Choosing a card opens its inputs, already set to the project's client and competitor websites; **Check** says whether it can run and what is missing or weak, and **Run** (once the project is unlocked with its password) starts it. Each run appears in a list with its status, cost and citation check; opening it shows its progress as it happens, the check of every citation and quote, and the result, which can be copied or downloaded.

**Cost and data safety.** Claude is switched off until you confirm which data may go to it. Until then each card produces a **context pack** instead: the full brief and the data, as a file you can give to Claude yourself where your policy allows. It costs nothing and sends nothing. Once Claude is switched on, a monthly cap must be set (no cap means no runs), and every call is checked in advance against the run, daily and monthly caps, so they cannot be crossed. A project can be marked confidential, and then none of its data is sent to Claude. Every run is kept with its brief, inputs, cost and checks.

### Step 17: Themes (what each website covers, and who owns each subject)

The **Themes** tab of a project groups all the pages of the client and its competitors into **themes** (subjects such as "term life insurance" or "secure printing") and, where a theme is broad, into narrower **topics**. Everything is worked out on this computer from the scraped pages: nothing is sent anywhere and it costs nothing.

**How pages are grouped.** Each page is described by the words in its title, main heading, breadcrumb, section headings, description and the start of its text; the title and heading count most. Words that appear on nearly every page of one website (its brand name, its footer) are ignored for that website. Pages that use the same distinctive words are grouped together, across all websites at once, so a client page and a competitor page about the same subject end up in the same theme. Each theme is named after the words that set it apart.

**What each theme shows.**
- How many pages each website has on it.
- **Who owns it**: the client owns it (at least one and a half times the pages of any competitor), a competitor leads it, it is contested, it is a **client gap** (competitors cover it and the client does not), or only the client covers it.
- The **pillar page** on each website: the page that anchors the subject (central to it, high in the site, an offering page in the catalog, and rich in content), with the reasons. The other pages are its supporting pages.
- Its topics, the matching offerings from the catalog, and the **funnel stage** of its pages: learning, comparing, deciding, getting started or getting help.

A workbook with every theme, the ownership table, topics and pages can be downloaded. When the map is built again later, a theme keeps its identity even as pages are added, so changes over time can be followed (which themes appeared, grew, shrank or changed owner). The analyst cards use the theme map too: the "Topic Cluster & Pillar Map" card starts from it.

On the John Hancock test project (272 pages, plus a 2-page competitor) the map took under a second: 9 themes, such as fund notices to shareholders (130 pages), monthly fund distributions (79), earnings releases (32) and the "Longer Healthier Better" podcast (18); every theme there is client-only except the competitor's security pages, which show as a client gap.

### Step 18: Monitoring (what changed, and what needs attention)

The **Monitoring** tab of a project compares each website's latest scrape with the previous one and says what changed and what an analyst should look at first. A check reads the scrapes already made: nothing is fetched and it costs nothing.

**What a check shows.** For the client and each competitor: the new pages (each placed in its theme), the pages that disappeared, and the pages that changed, with their new or removed text, title and heading rewrites, how much of the page changed, and **price changes** (an amount in the same sentence that changed, for example "$25 per month" becoming "$19 per month"). Lines that appeared or disappeared on many pages at once (navigation, "Read more", links to other articles) are listed once as template changes, so they do not make every page look changed. The theme section shows, for each theme, how many new pages each website added and whether its owner changed.

**Alerts**, most important first: a competitor adding several pages in one of the client's themes, a theme moving away from the client, a competitor changing prices, client pages disappearing; then a competitor building a theme the client does not cover, competitor title rewrites, pillar pages changing a lot; then smaller changes.

**What a rewrite did.** For each changed page the check also says how many words the main content had before and after, which H2/H3 headings were added or removed, which **terms** are new in the headings (for example "Generative", "AI", "Autonomous") and which were dropped, and whether tables, **pricing blocks** (an amount with "per month", "per user" and the like), FAQ answers or forms were added or removed. Three alerts come from this for competitors: a page **expanded by 500 words or more**, headings **shifted toward new terms**, and **new tables or pricing blocks** (high priority when it is pricing, or when the page is a pillar page).

**Automatic.** After each new scrape, the app runs a check by itself (on this computer, nothing is sent). A project can also be **scraped again on a schedule** (every 1, 3, 7, 14 or 30 days): that contacts the client's and competitors' websites, so it is off unless someone who unlocked the project turns it on. A workbook of every check can be downloaded, and the analyst cards read the alerts.

**Smart recrawl: not every page, every time.** A scheduled pass follows a **recrawl plan**. **Core pages** (pillar pages, pages in the header menu, comparison and decision pages, addresses naming pricing, plans, solutions, products and similar, the home page and pages one level from it, and any page a person pins; up to 50 per website) are checked on every pass. Other pages are checked once a month (adjustable), or sooner when the website's **sitemap** shows a newer change date than our last capture. Everything else is **carried forward**: no request is sent at all, and the page keeps its capture date so it comes due later. Pages that are checked still get the usual conditional request, so an unchanged page is never extracted again. The Monitoring tab can **preview** the plan (it only reads the websites' sitemaps), **recrawl now**, and lists for every page whether it will be checked and why; the plan is kept and downloadable. On John Hancock (303 addresses) a weekly pass would check 15 core pages and carry 288 forward; johnhancock.com refuses automated sitemap requests, and the plan says so and works without them.

On the John Hancock test project, comparing an earlier 107-page scrape with the 318-page one took 2 seconds: 206 new pages placed in their themes, 4 pages that really changed, and 9 template lines set apart. Testing on this real data removed false alarms: a change in how the engine splits pages, a title captured only in the newer scrape, a page still online but now marked as a duplicate, and tracking images are no longer reported as changes.

### Step 19: Research (search data: who ranks for what, and how many people search)

**Fixes from the November audit.** A website registered as the client's own website or brand in any project is the client's side everywhere: analyst cards never pick it as a default competitor, the Research tab never suggests it, and the gap analysis never flags it as an "unwatched competitor". Semrush spending cannot overshoot when two pulls run at once: each call's worst-case cost is reserved first, visibly to every other pull. Claude's cost cap prices cached input at the rate Claude actually charges, and a card run started from the command line waits its turn behind one started in the app. Wherever the engine reads a project's theme map (the Themes tool for Claude, research pulls and imports, the gap analysis) it checks whether any website was scraped since the map was built and rebuilds it first.


The **Research** tab of a project adds search data: for the client and each competitor, the keywords each website appears for in Google, its position, how many people search each keyword per month, and the page that ranks. The keywords are then placed in the project's themes, so each theme shows how much search demand it has and who wins it. When research is added again later, the tab lists the **movers**: keywords that moved three places or more, entered or left the first page, or appeared or disappeared.

There are two ways to add it:

- **Import a Semrush export (free).** In Semrush, export a website's "Organic Positions" list as a CSV file and upload it for that website. Nothing is sent anywhere.
- **Pull from Semrush (spends units).** The app asks Semrush directly for every website of the project. This uses Semrush API units, which cost money, so it is **off** until an operator sets the API key, switches it on and sets a **monthly unit cap**. Before a pull, the tab shows the most units it could use; every call is checked against the per-pull limit and what is left this month before it is sent, and every unit spent is logged. The pull budget is **shared fairly**: each website, client first, may use an equal share of what is left, so the first website cannot use up what the others need (with a small cap, each website simply gets fewer keywords, and the result says how many). Calls are **paced**, one every half second across the whole app, and if Semrush asks to slow down the app waits and tries again without paying twice. A website pulled in the last 14 days is **reused** instead of paid for again, unless "pull every website again" is ticked. Only website addresses and keywords are sent to Semrush, never page content.

**Suggested competitors.** The engine also notices competitors by itself. When search data comes in, it keeps every website that shows up competing with the client, from three places: Semrush's list of the client's organic competitors (part of every pull, about 800 units at most), a Semrush "Organic Competitors" export you upload (free), and the top 10 results of the keywords you pull. Each suggestion keeps how close it is (Semrush's relevance), the keywords it shares with the client, its own traffic, the keywords it appeared for, and when it was first and last seen; new pulls refresh the figures and add to them. The client's own websites are never suggested. A suggestion is only information: nothing is scraped or paid for until someone clicks **Add as competitor**, which adds it to the project (with a note saying where it was found); **Dismiss** hides it, and that decision is kept. The suggestions are also context elsewhere: the gap analysis lists close competitors that are not in the project yet ("Competitor not in project"), and Claude can read them (marked as not scraped, so it does not claim facts about their pages).

A workbook of every snapshot can be downloaded. Three analyst cards that needed search data (search intent and results, high-value keywords, keyword-to-page mapping) can now run on a project once it has research. Semrush has not been called for real yet: it waits for a key and a cap; the import works today.

### Step 20: Gaps (what the client is missing, most important first)

The **Gaps** tab brings the other modules together and lists what the client is missing compared with its competitors, ranked by priority:

- themes competitors cover and the client does not, themes where a competitor leads, themes without a pillar page on the client's side, and buying stages (learn, compare, decide, onboard, support) missing in a theme;
- with research, keyword by keyword, for every keyword a competitor ranks for near the top (top 10 by default) and enough people search (any number by default; both can be set in the tab), sorted by where the **client** stands:
  - **complete deficit**: the client does not rank in the top 100 at all. Only said when the client's search data is complete. A Semrush pull stops at a set number of keywords, and when the client has more than that the engine cannot be sure;
  - **probable gap**: not in the client's data, but that data was cut short. The finding says how much the data holds ("the top 200 of about 3,400 ranking keywords") and how to confirm;
  - **page far behind**: the client ranks 21 to 100, so it has a page to improve rather than one to write;
  - **page two**: the client ranks 11 to 20, the quickest wins. A deficit or far-behind finding mentions how many page-two keywords the same theme has.

  Each is grouped by theme and summed up the way an analyst would say it, for example "Complete deficit in Supply chain ESG: 180 keyword(s), 45,000 searches a month. coupa.com ranks for 120 of them with 25 page(s); rival.com for 60. The client does not rank in the top 100 for any of them." Every keyword is listed with the competitor's position, its searches and the client's position;
- with monitoring: competitors putting effort into a theme (adding pages);
- with the offering catalogs: products or services competitors offer that the client's catalog does not show (menu items such as "Read more" or "About us" are ignored).

Each finding says why, gives the evidence with links, how many searches a month are behind it when known, and a recommended next step. Its priority grows with how many competitor pages and how much search demand are behind it. The team can mark a finding **accepted**, **done** or **dismissed** with a note; the mark stays when the analysis is built again, and findings that are no longer a gap are listed as such, so progress can be followed. If research or monitoring is missing, the report says so and works with what it has. It runs on this computer and costs nothing; a workbook can be downloaded, and the analyst cards can read the findings.

On the John Hancock test project (with a printing-security competitor, no research yet) the analysis took under a second: 15 findings, one missing theme (the competitor's print-security subject) and 14 offerings the client does not have, as expected for two unrelated companies.

### Step 21: Working with Claude Desktop and claude.ai (no API key)

Two ways to use your own Claude account with the app, without an API key or API costs:

- **Claude Desktop, connected to the app.** Once set up (a few lines in Claude Desktop's settings, see `docs/app/MODULE_MCP_SERVER.md`), Claude Desktop can read every project while the app is running: the projects and their websites, page text and search, themes, monitoring alerts, search data and gap findings. You ask in plain words ("what are John Hancock's biggest gaps against Flynn's, and which pillar pages should we write next?") and Claude looks things up itself. It is **read-only**: it cannot scrape websites, pull from Semrush, change a finding's status or unlock anything; those stay buttons in the app. Every fact it gets comes with a reference, and before answering it can ask the app to **check its citations** (each reference must be real, each quote word for word), so you can see whether the answer passed.
- **"Open in Claude.ai"**, on every free context pack of an analyst card: one click copies the pack and opens a new chat on claude.ai in your browser; you paste it and send. That answer stays in claude.ai: the app does not check or save it.

**Readable answers.** Claude checks its sources with the app before answering, then shows you the checked answer in plain sentences with numbered sources ([1], [2]) and a list of the pages they come from, with links. The internal reference codes the check uses are never shown. Themes are named after their main page (for example "The Longevity Opportunity") instead of a list of keywords; this applies everywhere themes appear, from the next time a theme map is built.

**Answers laid out by type.** Claude now hands its answer to the app, which checks the sources and lays it out with a template for the kind of answer: a quick answer, an overview (with a table), findings grouped by priority (red, orange, blue), a side-by-side comparison, a page review (what works, what to fix), what changed, or a keyword report. Every answer starts with a title and an "In short" summary, ends with next steps, a "Sources checked" line and numbered, linked sources. Ready-made requests ("Gap report", "Page review", "What changed", "Theme overview" and others) appear in Claude Desktop's prompt menu; a module's connector offers only its own.

**Staying in one module.** If a chat should use only one module (for example Themes) and nothing from the others, add the module connectors as well (one line in the setup prints them all) and switch on only that module's connector in the chat: Claude then cannot see or call the other modules, and the app refuses them even if it tried. With the full connector, saying "only use Themes" is also respected, as an instruction.

Both send the project's data to Claude through your account, so they are for clients whose data may go to Claude (the data policy question is still open). Search now also works when the local meaning model is off: it falls back to exact words and says so.

### Step 22: Downloads (every report, in Excel, CSV or Word)

Everything the modules show can be taken away as a file. Each module tab (Themes, Monitoring, Research, Gaps, and the Suggested competitors panel) has a **Download** button with three choices:

- **Excel (.xlsx)**: every row and the full text, one sheet per table. The file to work in.
- **CSV**: the same tables for other tools (one file, or a zip with one file per table).
- **Word (.docx)**: the report laid out for reading and sharing: a title, what it is and when its data was made, a short summary, then each table. Word is not made for thousands of rows, so a long table shows its first 300 rows and a very long cell its first 600 characters, each time with a line saying so; the Excel file always has everything.

A project's **Downloads** tab lists every report at once (project overview with its websites and scrapes, themes, monitoring, search data, suggested competitors, gaps, analyst card runs), says which are ready and, for the others, what to do first ("Build the theme map in the Themes tab first"), and has **Download everything (zip)**: every ready report in all three formats, with a short read-me. Each analyst card result can also be downloaded as a Word document next to the Markdown file. Reports are made on request from each module's latest results: nothing is scraped, pulled or paid for to make them. The page-by-page scrape reports stay in the Reports and Project reports tabs.

## 5. Safety and cost promises

- Read-only: the engine never submits forms, logs in, buys, unsubscribes or clicks anything that changes the website.
- Polite: robots rules and requested delays are respected; a struggling site is paused, not hammered.
- Free by default: every run starts at $0. Paid access (Bright Data) needs your explicit approval per website, with caps of $5 per run and $25 per day, a monthly cap and a kill switch.
- The Claude Desktop connection is read-only and local: it reaches the app only on this computer, cannot scrape, spend or change anything, and no web page can use it.
- Semrush pulls are off until an operator sets a key and a monthly unit cap; each pull is checked against the per-pull and monthly limits before the first request, and every unit is logged. Importing a Semrush export is always free.
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
| Automated tests | About 1,300 tests passing (engine, app, modules and the web UI), 93% of the code exercised by tests. A CI pipeline runs all quality checks and the release gate on every change once the code is on GitHub. |
| Contradiction report on John Hancock | After the 20 October fix: 0 false contradictions (previously 17); differing-context pairs listed separately. |

## 7. What it does not do yet

- Paid access through Bright Data's remote browser is not built; pages fetched through paid access are read from their page code, not opened in a browser through the paid connection.
- Durable database: the PostgreSQL storage exists but has never been tested against a real database.
- Page-by-page review of project scrapes: the analyst web app (pages, duplicates, contradictions, reviewer decisions) only reads runs started from the command line; project scrapes have their own live view and reports but no reviewer decisions yet. The app has no login: it is safe on one computer or a private network, not on the open internet.
- Some content types: text drawn on canvas or in CSS, combinations of product options (each option is captured on its own, never every combination), audio and video, archives and email files, and old-style .doc and .xls files.
- Semantic similarity in the reports: the knowledge base can search by meaning, but the duplication and contradiction reports (per website and per project) still compare wording. A Semantic module (duplication and contradictions by meaning, raw content for Claude) is planned and needs Ollama with bge-m3 installed first.
- Action cards: Claude stays off until the data policy is confirmed, so no card has been run through Claude on real data yet; the Claude part is tested against a simulated Claude only. The three keyword cards run once a project has research (a Semrush export can be imported for free); a live Semrush pull has not been made yet.
- The rest of the knowledge base: a screen that compares several projects at once is not built yet (gap findings are per project; stamping, the offering catalog, section labels, search and questions are done). The AI steps (embeddings with bge-m3, labels and answers from a local model) have been tested against a simulated server only, and the Postgres vector store only in automated tests, until Ollama and Postgres with pgvector are installed; until then the local file and the word-matching stand-in are used.
- Hidden page text: some text present in the page code (for example frequently-asked-question answers) is not yet revealed and captured by the browser exploration; the engine flags these pages for review rather than counting them complete.
- Pages scraped before a rule was corrected keep their old verdict until they are re-scored (`reverdict`) or scraped again; a repeat scrape reuses an unchanged page's earlier record, so clearing an old verdict on an unchanged page needs the re-score.

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
| 2026-11-10 | Images are downloaded once: the browser's own download of each image is kept, with the same checks, evidence and reading step as before, and anything the browser did not load is downloaded as before. About 85% fewer requests to a site per batch and 3 to 4 minutes saved per batch of 25 pages. |
| 2026-11-10 | Faster without changing results: documents and images are read while pages are still being fetched and rendered; file readers stay running instead of starting a new program per file; the text-recognition program is probed once per run; end-of-batch screenshot reading runs several pages at once; the engine waits exactly the courtesy gap between requests. Records on the test sites are identical before and after. |
| 2026-11-10 | Access rules corrected: missing pages (404/410) are never content; soft-404 needs whole words and body text; a header login form is not a login wall; scripts are not text; security cookies do not turn errors into bot checks; app shells go to the browser first; a blocked page escalates alone and a site only after three blocked pages. Gates tightened: audience gates need a pop-up with a button; lead-gen walls are judged per section and never over a whole article. |
| 2026-11-10 | Live scrape view: batches, the pages of a batch with stage, result and "content captured", and the live log. Sleep no longer counts as scrape time; pages a batch ran out of time for are retried; report and knowledge steps stop on cancel, on a time limit and when the app closes; one scrape per website even under simultaneous requests; deleting a project removes every module's data for it. |
| 2026-11-10 | Confidential projects (create form and Settings): page text never goes to Claude or Claude Desktop. A client's brands are never competitors in any project; Semrush caps hold under concurrent pulls; Claude caps price caching correctly; theme maps are rebuilt after a newer scrape. |
| 2026-11-10 | Server-ready: configurable bind address, allowed hosts and origins; bounded run cache; project reports built in a helper process; a Dockerfile and deployment notes with memory guidance. |
| 2026-11-10 | Tag-manager and pixel frames, drop-down choices and SVG logos no longer make a page "partly captured"; a `reverdict` command re-scores the pages already scraped without fetching them again. The number of images read at once is a setting (`CP_DOC_PARALLEL_TASKS`). Web UI types, tests and build now run in CI. |
| 2026-11-09 | A stopped scrape continues where it stopped: every response is saved as it arrives and is not requested again, with a Continue button for cancelled, failed or interrupted scrapes. Scrapes are faster: a site's repeating menus are explored only on the first pages (a GEP page in the browser went from about 36 to about 20 seconds with identical article text) and four images are read at a time. |
| 2026-11-09 | A CAPTCHA box on a page's form no longer makes the page "blocked"; only a real challenge page does (an error answer, a "prove you are human" page, or a page with almost nothing else on it). |
| 2026-11-09 | Claude Desktop can export every page of a theme as a CSV file (export_theme_pages_csv) and check the scrape queue (get_scrape_queue): what is running, waiting or ready, with progress. Both only read; neither starts or stops a scrape. |
| 2026-11-09 | Claude Desktop can list every page of a theme in full (new tool get_theme_pages): the theme is chosen by its id or name, and nothing is cut short. It reads the project's saved theme map and does not touch the web app or running scrapes. |
| 2026-11-09 | Meaning without a model server: when Ollama is not available, search and the knowledge base use a small multilingual model on this computer's processor (downloaded once, free, offline) before falling back to word matching; a store is rebuilt by itself when a better model arrives and never downgraded; search says per website which model answered. John Hancock: 1,308 sections in 23 seconds. |
| 2026-11-05 | Model servers: an in-house Ollama is accepted by its private address or by a name that points only to private addresses; the company's own cloud server is accepted only when switched on and over https (plain http to the internet is always refused, with the reason); an optional access key for servers behind a sign-in gateway. |
| 2026-11-05 | Semrush pulls are paced (one call every half second across the app, waiting and retrying when Semrush asks to slow down, without paying twice), share the pull budget fairly between websites, and reuse websites pulled in the last 14 days instead of paying again (a checkbox pulls everything again). |
| 2026-11-04 | Keyword gaps by the client's position: complete deficit (not in the top 100, only when the client's data is complete), probable gap (the client's data was cut at the pull limit, with how much it holds), page far behind (21-100) and page two (11-20), each summed up per theme and competitor ("coupa.com ranks for 120 of them with 25 pages"); minimum searches and competitor rank can be set per project. |
| 2026-11-04 | Smart recrawl and richer change alerts: scheduled re-scrapes follow a plan (core pages every pass; other pages monthly or when the sitemap shows a change; the rest carried forward with no request), with a preview and "recrawl now" in Monitoring and the plan kept and downloadable. Each changed page now shows words before and after, headings added and removed, new and dropped heading terms, and new tables, pricing blocks, FAQs and forms; competitors get alerts for big expansions, heading shifts and new tables or pricing. |
| 2026-11-04 | Downloads everywhere: a Download button in every module (Excel, CSV or Word) and a Downloads tab per project listing every report (overview, themes, monitoring, search data, suggested competitors, gaps, analyst runs) with what each needs first, plus everything in one zip; analyst card results also as Word. Made from the latest results, at no cost. |
| 2026-11-04 | Competitors: creating a project asks who the client competes with (once more if none is named). The engine keeps the competitors the search data reveals (Semrush organic competitors in every pull, a free Semrush competitors export, the top 10 of pulled keywords) as suggestions with their figures; add one to the project or dismiss it in the Research tab. Close competitors not in the project appear in the gap analysis, and Claude can read the suggestions. |
| 2026-11-04 | Project reports include the full page-by-page project report (Pages with whole content, Matching content with the shared text) for all of the client's websites; builds run in the background. |
| 2026-11-03 | Answer templates in Claude Desktop: the app lays out each checked answer by type (answer, overview, findings by priority, comparison, page review, what changed, keywords) with a title, an "In short" summary, next steps, a sources-checked line and linked sources; seven ready-made requests in Claude Desktop's prompt menu. |
| 2026-11-03 | Readable answers in Claude Desktop: checked answers come back with numbered sources and links instead of reference codes, and Claude no longer shows internal ids. Themes are named after their main page instead of a keyword list, in every screen and report. |
| 2026-11-03 | Project reports: one duplication and contradiction report across all of the client's websites (main website and brands), from their latest scrapes. Contradictions grouped by topic (Topic, Version A/B, where each appears, URLs, severity, recommended action, approved answer to fill), with news and dated figures and condition-dependent differences on their own sheets; duplication marks the same text across websites; competitors only on request, in their own workbook. Rules only, $0. |
| 2026-11-03 | Claude Desktop, one module at a time: a connector per module (content, themes, monitoring, research, gaps) shows Claude only that module's tools and status, and the app refuses other modules on it; with the full connector, "only use this module" is respected as an instruction. |
| 2026-11-03 | Home screen: every module as a tile (Content Scraper, Knowledge search, Themes, Monitoring, Research, Gaps, Analyst cards) with how many projects have results; each module lists the shared projects with their state in that module, creates new ones, and opens a project on that module's tab. Modules also in the left menu; All runs has its own entry. |
| 2026-11-03 | Shared scrape queue: projects and addresses stay unlimited, but one scrape runs at a time for everyone. Each queued scrape starts automatically or emails its person when its turn comes (then runs next once started); optional email when it finishes; a Scrape queue page with places in line and estimated waits. |
| 2026-11-03 | Claude Desktop connection: a read-only MCP server lets Claude Desktop read every project (search, pages, themes, monitoring, search data, gaps) through the running app and check its own citations before answering; no API key. "Open in Claude.ai" on context packs copies the pack and opens a new claude.ai chat. Search falls back to exact words when the local meaning model is off. |
| 2026-11-02 | Gaps: one ranked list of what the client is missing against its competitors (themes, pillar pages, buying stages, keywords, page-two keywords, competitor momentum, offerings), each with evidence, demand and a next step; the team's decisions (accepted, done, dismissed) carry over and closed gaps are listed. Built on this computer at no cost. |
| 2026-11-02 | Research: search data per website (keywords, positions, searches per month, ranking page), placed in themes for demand per theme, with movers between snapshots; free import of a Semrush export, or a Semrush pull that is off until a key and a monthly unit cap are set, checked against caps and logged. The three keyword analyst cards can now run. |
| 2026-11-02 | Monitoring: each check compares the latest scrapes with the previous ones (new, removed and changed pages, rewrites, price changes, template changes), places new pages in their themes, follows theme ownership, and ranks alerts; it runs by itself after new scrapes; scheduled re-scraping is an opt-in per project. |
| 2026-11-02 | A project can hold several websites of the client (brands, regional sites, sections of one domain); they are always the client's side and never competitors of one another, in theming, gap findings, search and analyst cards, across all projects. Every website can carry a note and tags, editable at any time. |
| 2026-11-02 | Themes: each project's pages, client and competitors together, grouped into themes and topics, with the pillar page on each website, who owns each theme (client, competitor, contested, client gap), funnel stages and a workbook; built on this computer at no cost, and followed over time as the map is rebuilt. |
| 2026-11-02 | Analyst cards in the app: a tab on every project to choose a card, check it, run it (after unlocking the project), follow it live, and read, copy or download the result with its citation check. |
| 2026-11-02 | Senior Analyst Action Cards: 15 ready-made SEO analyses (trust audit, cannibalization, technical fixes, AI answer readiness, topic clusters, funnel, offering matrix, competitor radar, content refresh, head-to-head gap, content brief, executive briefing; 3 keyword cards wait for Semrush). Each gives Claude a detailed brief and the project's data, and every statement in the result is checked against the pages and listed with its proof. Claude is off until the data policy is confirmed; meanwhile each card produces a free context pack. Spending caps per run, day and month. |
| 2026-10-31 | Projects hold the client's website and competitor websites, each stored on its own (addresses, scrapes, reports, searchable knowledge). One list can fill several websites at once. Search across websites, by default the client and every competitor known in any project, each website searched once in its most recent scrape. |
| 2026-10-30 | Searchable knowledge base: "index" stores every page section with its meaning (local bge-m3 model, or a word-matching stand-in until Ollama runs), its exact text and its evidence, in Postgres with pgvector or a local file, re-using unchanged sections; sections are labelled by role and product only when two independent signals agree; "search" combines meaning and exact words; "ask" answers with statements each checked against a word-for-word quote and linked to the page; "kb-check" says what is ready. John Hancock: 1,308 sections in about 10 seconds, $0. |
| 2026-10-30 | Content Scraper app (Module 1): password-protected projects, addresses pasted or imported from a file, scraping started and followed in the browser, every report downloadable (one by one or as a zip). |
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
