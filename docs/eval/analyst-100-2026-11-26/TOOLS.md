## list_websites
The websites this run may read (client and competitors), with whether each is searchable, page and passage counts, and the embedding model.
input: {"additionalProperties": false, "properties": {}, "type": "object"}

## search_knowledge
Hybrid search (meaning plus exact words) over the websites' captured text. Returns verbatim passages with a `ref` to cite, each with the text around it on its page (`before` / `after`, same section first, each part with its own `ref`). Use per_site for side-by-side comparisons and role to keep only claims, pricing, proof points, FAQs, CTAs or disclaimers.
input: {"additionalProperties": false, "properties": {"query": {"description": "What to look for", "maxLength": 300, "minLength": 1, "title": "Query", "type": "string"}, "websites": {"anyOf": [{"items": {"type": "string"}, "maxItems": 20, "type": "array"}, {"type": "null"}], "default": null, "description": "Domains to search; omit for the whole scope", "title": "Websites"}, "per_site": {"anyOf": [{"maximum": 10, "minimum": 1, "type": "integer"}, {"type": "null"}], "default": null, "description": "Best passages per website (side by side)", "title": "Per Site"}, "top_k": {"default": 8, "description": "Passages overall (without per_site)", "maximum": 25, "minimum": 1, "title": "Top K", "type": "integer"}, "role": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "description": "Only passages labelled with this role: claim, feature, pricing, proof_point, faq, cta, disclaimer", "title": "Role"}, "context_tokens": {"anyOf": [{"maximum": 20000, "minimum": 0, "type": "integer"}, {"type": "null"}], "default": null, "description": "Tokens of text returned in all (passages plus the text around them, about 4 characters each); default CP_SEARCH_CONTEXT_TOKENS; 0 = the passages alone", "title": "Context Tokens"}}, "required": ["query"], "type": "object"}

## get_page
Every captured section of one page in reading order (each with a `ref`), plus the page's facts: title, description, headings, structured data, CTAs, dates.
input: {"additionalProperties": false, "properties": {"url": {"description": "The page's full address", "maxLength": 2000, "minLength": 8, "title": "Url", "type": "string"}}, "required": ["url"], "type": "object"}

## get_page_facts
One page's facts only (no text): title, meta description, H1, canonical, headings, schema types, FAQ questions, offers, CTAs, numbers, dates, word count.
input: {"additionalProperties": false, "properties": {"url": {"description": "The page's full address", "maxLength": 2000, "minLength": 8, "title": "Url", "type": "string"}}, "required": ["url"], "type": "object"}

## list_pages
Every captured page of one website: title, H1, page type, offering, path, word count and dates (up to 300 pages).
input: {"additionalProperties": false, "properties": {"website": {"description": "Domain, e.g. example.com", "maxLength": 300, "minLength": 3, "title": "Website", "type": "string"}}, "required": ["website"], "type": "object"}

## get_offering_catalog
The website's offering tree (families, offerings, sub-offerings) from its menus, addresses and structured data, with the pages mapped to each and card attributes.
input: {"additionalProperties": false, "properties": {"website": {"description": "Domain, e.g. example.com", "maxLength": 300, "minLength": 3, "title": "Website", "type": "string"}}, "required": ["website"], "type": "object"}

## get_seo_issues
Technical SEO/GEO issues of a website's pages, grouped by issue with severity, page counts and example addresses (titles, descriptions, canonicals, headings, structured data, images, links). The first call may take a minute.
input: {"additionalProperties": false, "properties": {"website": {"description": "Domain, e.g. example.com", "maxLength": 300, "minLength": 3, "title": "Website", "type": "string"}}, "required": ["website"], "type": "object"}

## find_similar_pages
Pairs of the website's pages whose content is nearly the same (cannibalization and duplicate candidates), with titles, page types and offerings.
input: {"additionalProperties": false, "properties": {"website": {"maxLength": 300, "minLength": 3, "title": "Website", "type": "string"}, "min_similarity": {"default": 0.85, "maximum": 0.99, "minimum": 0.5, "title": "Min Similarity", "type": "number"}}, "required": ["website"], "type": "object"}

## get_changes
What changed on a website between its last two scrapes: new and removed pages, title/H1/description rewrites, added and removed text, new numbers (prices, rates).
input: {"additionalProperties": false, "properties": {"website": {"description": "Domain, e.g. example.com", "maxLength": 300, "minLength": 3, "title": "Website", "type": "string"}}, "required": ["website"], "type": "object"}

## get_theme_map
The project's theme map: themes (topic clusters) across the client and its competitors, each with its pages per website, pillar page per website, topics, funnel stages and ownership (client_owned, contested, competitor_led, client_gap, client_only). Built offline from the scrapes; reuses the latest build.
input: {"additionalProperties": false, "properties": {"rebuild": {"default": false, "description": "Build it again from the latest scrapes", "title": "Rebuild", "type": "boolean"}}, "type": "object"}

## get_monitoring_alerts
The project's latest monitoring check: alerts ranked by severity (competitor pushes into client themes, new competitor themes, ownership shifts, price changes, removed pages, rewrites, pillar drift), what changed on each website, and theme drift. Runs a check when none exists.
input: {"additionalProperties": false, "properties": {"run_check": {"default": false, "description": "Run a new check first (compares the latest scrapes)", "title": "Run Check", "type": "boolean"}}, "type": "object"}

## get_gap_findings
The project's gap findings, highest priority first: themes the client misses or trails in, missing pillar pages and funnel stages, competitor keywords by the client's position (complete deficit, probable gap, far behind, page two), competitor momentum, offerings and competitors not in the project; each with evidence, a recommended action and its status (dismissed findings are left out).
input: {"additionalProperties": false, "properties": {"kind": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "description": "Only this kind: theme_gap, theme_behind, pillar_gap, funnel_gap, keyword_gap (complete deficit: the client is not in the top 100, its data complete), keyword_unverified (probable gap: the client's data was cut short), keyword_behind (the client ranks 21-100), striking_distance (11-20), competitor_momentum, offering_gap, unwatched_competitor, link_intersect (websites linking to competitors and not to the client), authority_gap (a competitor's much stronger links)", "title": "Kind"}, "limit": {"default": 40, "maximum": 200, "minimum": 1, "title": "Limit", "type": "integer"}, "rebuild": {"default": false, "description": "Build the gap analysis again first", "title": "Rebuild", "type": "boolean"}}, "type": "object"}

## get_keyword_rankings
Keywords a website ranks for in the latest research snapshot (Semrush pull or export): position, previous position, volume, CPC, ranking URL, theme and estimated visits. Filter by theme or position.
input: {"additionalProperties": false, "properties": {"website": {"maxLength": 300, "minLength": 3, "title": "Website", "type": "string"}, "theme_id": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": null, "description": "Only keywords of this theme", "title": "Theme Id"}, "max_position": {"default": 100, "maximum": 100, "minimum": 1, "title": "Max Position", "type": "integer"}, "limit": {"default": 100, "maximum": 500, "minimum": 1, "title": "Limit", "type": "integer"}}, "required": ["website"], "type": "object"}

## get_serp_overview
Who ranks for a keyword: from the snapshot's search results when the keyword was pulled, else every website of the project that ranks for it, with position and URL.
input: {"additionalProperties": false, "properties": {"keyword": {"maxLength": 200, "minLength": 1, "title": "Keyword", "type": "string"}}, "required": ["keyword"], "type": "object"}

## get_keyword_metrics
Volume, CPC and competition of a keyword, and keywords of the snapshot that contain it (related demand), from the latest research snapshot.
input: {"additionalProperties": false, "properties": {"keyword": {"maxLength": 200, "minLength": 1, "title": "Keyword", "type": "string"}}, "required": ["keyword"], "type": "object"}

## get_search_demand
Search demand per theme of the project: keywords, monthly volume, and each website's keywords in the top 10 and estimated visits.
input: {"additionalProperties": false, "properties": {}, "type": "object"}

## get_page_report
The client website's page report: per page the keywords it ranks for, monthly searches, estimated visits, average position, links from other pages' text, internal PageRank, word count and on-page problems; plus cannibalisation (pages competing for one keyword), pages stuck at 4-20 with on-page problems, internal link suggestions, Search Console CTR opportunities, pages with no links from page text and pages that rank for nothing. Ask one section at a time.
input: {"additionalProperties": false, "properties": {"section": {"default": "summary", "description": "summary (counts and the top pages), pages (per-page rankings, links, health), cannibalisation, stuck (ranking 4-20 with on-page problems), links (internal link suggestions), search_console (CTR opportunities), unlinked (pages with no links from page text), zero_traffic (scraped pages that rank for nothing), consolidation (the cannibalisation plan: page alignment, competing pairs with their consolidation pattern and fix, link and technical changes)", "enum": ["summary", "pages", "cannibalisation", "stuck", "links", "search_console", "unlinked", "zero_traffic", "consolidation"], "title": "Section", "type": "string"}, "limit": {"default": 25, "maximum": 200, "minimum": 1, "title": "Limit", "type": "integer"}}, "type": "object"}

## get_competitor_suggestions
Competitors the search data reveals (Semrush organic competitors, competitors exports and the top 10 of pulled keywords): relevance, keywords in common, traffic, example keywords, and whether each is already a website of the project.
input: {"additionalProperties": false, "properties": {}, "type": "object"}

## get_backlinks
The project's backlink data (Ahrefs pull or export): each website's Domain Rating, Ahrefs rank and backlink counts, the authority gap against each competitor, the link intersect (websites linking to competitors and not to the client, strongest first) and the client's most linked pages; give a website to list its strongest referring domains too.
input: {"additionalProperties": false, "properties": {"website": {"anyOf": [{"maxLength": 300, "type": "string"}, {"type": "null"}], "default": null, "description": "Also list this website's strongest referring domains", "title": "Website"}, "limit": {"default": 25, "description": "Rows per list", "maximum": 200, "minimum": 1, "title": "Limit", "type": "integer"}}, "type": "object"}

