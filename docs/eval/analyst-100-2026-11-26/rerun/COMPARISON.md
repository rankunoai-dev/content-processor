# Re-run after the analyst tool fixes (same 100 questions, same rules, 26 Nov 2026)

Same questions, same John Hancock project (client site only, one scrape, no competitors or search data), same brief; the tools came from the integration branch with the fixes. The project's stored stamps, passage labels and index were **not** rebuilt, so the duplicate and passage-role fixes are not in this run.

| | Answered | Partial | Not answered |
|---|---|---|---|
| Before | 3 | 32 | 65 |
| After | **14** | 24 | 62 |

Reasons for the rest: no data in the project 59 -> 61, tool gap **22 -> 8**, not collected 16 -> 17.

**17 better:** 1 missing descriptions, 4 duplicate titles, 5 noindex, 10 multiple H1, 15 thin pages, 40 FAQ schema, 59 mixed content, 70 Organization schema, 72 title = H1, 74 parameter URLs, 82 soft 404s, 88 BreadcrumbList (all now answered); 14 redirect chains, 24 orphan pages, 35 broken internal links, 45 5xx errors, 96 glossary page (now partial).

**3 worse:** 67 disclaimers (the role filter still finds nothing: labels change only when the knowledge space is rebuilt), 69 quick wins (graded stricter: no traffic data to rank them), 81 image formats (not collected; the tester also hit the test wrapper's 12,000-character cut, raised to the engine's 40,000 during this run).

**Still missing (tool gaps left):** the offending element in issue lists (which image, which second H1), how many scraped pages link to an unscraped address, a "every page that mentions X" consistency view, crawl depth, X-Robots-Tag headers, image size and format, external link status.

**Biggest lever is still data:** the scrape has no product pages (the catalogue lists term, permanent, LTC, Vitality, annuities with 0 pages), and the project has no competitors, Semrush, Ahrefs, Search Console or GA4.
