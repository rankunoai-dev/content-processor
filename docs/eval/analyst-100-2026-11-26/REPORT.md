# 100 random SEO analyst questions vs the engine (John Hancock, 26 Nov 2026)

## How it was run

- **Questions:** written by an agent that knew nothing about the engine ("you are a senior SEO analyst; John Hancock is your client; write 100 varied questions"): technical, on-page, content, competitors, rankings, links, AI answers, analytics, compliance, reporting. [questions.txt](questions.txt)
- **Answering:** four agents, 25 questions each, acting as Claude Desktop: only the engine's 19 read-only tools ([TOOLS.md](TOOLS.md)) through [tool.py](tool.py), no code, no stored files, no web, no own knowledge. Each graded itself (answered / partial / not answered, and why). Rules: [TESTER_BRIEF.md](TESTER_BRIEF.md). Every answer: [results_table.md](results_table.md), raw: `results_*.jsonl`.
- **Data in the project:** johnhancock.com only, one scrape (26-27 Oct): 303 addresses, 272 searchable pages, mostly fund press releases, podcasts and annuity e-signature help forms. No product pages, no competitors, no search data, no backlinks, no second scrape.
- **Checked by hand afterwards:** the full outputs behind the testers' main claims (see "Corrections").

## Scorecard

| Result | Questions |
|---|---|
| Answered | **3** (6 SEO-issue summary, 55 long titles, 69 three quick wins) |
| Partial | **32** |
| Not answered | **65** |

Why the 97 others fell short:

| Reason | Questions | Meaning |
|---|---|---|
| No data in the project | **59** | a tool exists but this project has nothing for it (competitors, Semrush, Ahrefs, Search Console, a second scrape, product pages) |
| Tool gap | **22** | the information is (or could be) in the scrape, but no tool gives it well enough |
| Not collected | **16** | the engine does not gather this at all |

**Read it this way:** the low score is mostly the project, not the engine. About 6 in 10 questions need data the engine can already hold. With the product pages, two or three competitors (a lite scan is enough), a Semrush export, an Ahrefs pull and a second scrape, most of those 59 would have a tool and data to answer them. The engine's own shortfall is the 22 tool gaps (fixable in code) and the 16 questions that need new data sources.

## What the engine cannot do today (the 38 that are not about missing data)

### Tool gaps: in our data or close to it, fixable (22)

1. **Lists stop at examples.** `get_seo_issues` gives counts and only 5 example pages per issue; `list_pages` returns 37 of 318 pages, with no paging or filters. So "which pages are missing descriptions / have two H1s / are under 300 words / have no alt text" get a number, not the list (Q1, 10, 15, 19, 34, 72).
2. **Technical checks that do not exist:** duplicate titles (Q4), redirect chains (Q14), HTTP status codes 404/5xx and soft 404s (Q45, 82), broken internal links (Q35), robots.txt rules and sitemap membership (Q28, 29), X-Robots-Tag on PDFs (Q91), mixed content and parameter URLs (Q59, 74), title equal to H1 (Q72), hreflang (Q50). Some of these (status codes, final addresses after redirects) are already in the scrape records; they just have no tool.
3. **The page report needs search data for everything.** Internal links, unlinked (orphan) pages and link suggestions are refused until a Semrush or Search Console import exists, though they only need the scrape (Q24, 57, 85).
4. **Schema shows type names only.** No properties, so Organization `sameAs`/logo and BreadcrumbList cannot be checked, and there is no site-wide schema count (Q70, 88). `faq_questions` came back empty on pages with visible FAQ accordions (Q40).
5. **Search role filters are empty.** `search_knowledge` with `role: disclaimer/claim/pricing/...` always returns nothing: none of the 1,308 stored passages has a role label (confirmed). Disclaimers and claims questions fall back to plain search (Q67, 86, 93).
6. **No author/byline or "above the fold" fields** (Q30, 41).

### Not collected (16)

Analytics traffic, conversions, bounce and leads (Q16, 32, 49, 89, 92); Core Web Vitals, page speed, mobile usability, image weight and formats (Q26, 64, 80, 81); Google index coverage (Q46); AI assistant answers and citations (ChatGPT, Perplexity, AI Overviews: Q17, 27, 58, 94); local listings (Q53); status of external links (Q98).

## Corrections and findings from checking by hand

- **The 12,000-character cut was the test wrapper, not the engine.** Seen in full, `get_seo_issues` has 16 issue types; nothing the testers missed is in the cut part except three small ones (skipped heading levels, video captions, long descriptions). The real limits are the 5 example pages per issue and `list_pages` returning 37 pages. The Claude Desktop connector does not cut tool output.
- **A real bug found:** the two "Income Made Easy (IRA and non-qualified)" e-signature form pages (client and rep copies) were both excluded as duplicates of each other, so neither is searchable (0 passages). Duplicate exclusion should keep one copy. This is why a search for the exact title found nothing; it is not the reranker.
- **Content problems on the client's site the testers spotted:** the chat widget's "John Hancock Virtual Assistant" is read as the H1 on podcast pages (much of the 262 "more than one H1"); `/errors` is live, self-canonical and has lorem-ipsum headings; 17 noindexed pages are podcast episodes that look worth indexing; a staging host `author-stg-tmp.johnhancock.com` appears in the inventory.
- **Speed:** searches took 3-30 s alone and up to 3 minutes with four testers at once.
- Self-grading is generous in places ("partial" for a count without the list); the tool gaps above are confirmed against full outputs.

## What would move the score most

1. **Data, no code:** scrape the product pages (term, permanent, Vitality, LTC, annuities: catalogued from the menus but 0 pages captured), add 2-3 competitors (lite scan), import a Semrush export, run an Ahrefs pull, scrape again in a week. Unlocks most of the 59.
2. **Full lists with filters** in `get_seo_issues` and `list_pages` (paging, by issue, by word count, by date). Turns about 8 partials into answers.
3. **A technical-checks tool from the records already stored:** status codes, redirects, soft 404s, duplicate titles, title = H1, parameter URLs, X-Robots-Tag, broken internal links. About 10 questions.
4. **Internal-link parts of the page report without search data.** 3 questions.
5. **Passage roles** actually labelled (disclaimer, claim, pricing...), and the duplicate-exclusion fix.
6. Later, new sources: Search Console and analytics imports, a Core Web Vitals pull (PageSpeed API), AI-answer tracking.

## Rerun

From the repo root: `python docs/eval/analyst-100-2026-11-26/tool.py <tool> '<json>'` (tool list: `--list`). Same questions and brief; compare against this scorecard after the data above is added.
