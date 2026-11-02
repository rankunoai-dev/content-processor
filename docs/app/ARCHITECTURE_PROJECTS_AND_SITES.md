# Architecture: projects, websites (client and competitors) and the knowledge the bot uses

Status: decided 2026-10-31 with the product owner; first build on `feat/app-project-sites`. Extends `MODULE_01_CONTENT_SCRAPER.md`.

## 1. The requirement

> When a project is created, the person may already know several websites: the client's and its competitors', with all their URLs. All of this goes into one project, but **each website gets its own storage, stored separately**, so that later the **bot can refer to them and work across websites and across projects**.

## 2. The model

```
Project  (name, owner, client, password)            one per engagement
 └── Website  (name, role, domains)                 the client's site + one per competitor
      ├── URL list                                  this website's addresses only
      ├── Scrapes (runs)                            this website's runs, reports
      └── Knowledge space                           this website's chunks, vectors, labels
```

| Term | Meaning |
|---|---|
| **Project** | One engagement. Holds the password; everything below belongs to it. |
| **Website** (site) | One web presence inside a project. Role `client` (the client's main website, plus any other websites or brands of the client) or `competitor` (any number). Has one or more **addresses** (`flynns.com`, where `blog.flynns.com` counts as the same site, unless the project has a separate website at `blog.flynns.com`), a **note** and **tags** (6b). |
| **Knowledge space** | The searchable store of one website in one project: its chunks, vectors, page vectors, labels and offering catalog. Name: `<tenant>.<project id>.<site id>`. |
| **Scope** | A list of knowledge spaces the bot is allowed to read for one question (one website, a whole project, or several projects). |

## 3. Storage: what is separate and what is shared

| Data | Where | Separate per website? | Why |
|---|---|---|---|
| Project (name, owner, client, password hash, list of websites) | `app_data/projects/<project>/project.json` | per project | |
| Website (name, role, domains) | `…/sites/<site>/site.json` | **yes** | |
| URL list | `…/sites/<site>/urls.json` | **yes** | |
| Scrapes: batch work files, merged records, reports | `…/sites/<site>/runs/<run>/` | **yes** | Each website's reports stand alone |
| Knowledge: chunks, vectors, labels, offerings | knowledge store, namespace `<tenant>.<project>.<site>` (SQLite file `knowledge_store/<namespace>/vectors.sqlite`, or the same namespace as the tenant column in Postgres) | **yes** | The bot reads only the spaces in its scope; one competitor's catalog never mixes with another's |
| Evidence (original pages, screenshots) | `evidence/<tenant>/`, content-addressed | shared | Identical bytes are stored once; nothing in a file says which project asked, so sharing cannot mix content |
| URL history (last capture of each URL) | `url_history/<tenant>/` | shared | The same page scraped for two projects is reused instead of scraped twice; reuse is still decided per run |

Deleting a website deletes its folder and its knowledge space; evidence other websites use stays.

## 4. Adding URLs when several websites are known

- **At creation**: the client's website (name and address) and any number of competitors (name and address each). The address gives the website its domain.
- **One list for everything**: a paste or a file with URLs of several websites can be added at **project** level; each URL goes to the website whose domain it belongs to. A URL of a website that is not in the project is not added; the result names its domain and suggests adding it as a competitor.
- **Inside one website**: URLs of another domain are refused with the name of the website they belong to.
- A website created without an address learns its domain from its first URLs.

## 5. Scraping and the knowledge step

A scrape belongs to one website. "Scrape all websites" queues one scrape per website. After a scrape's reports are written, the runner builds that website's knowledge space:

1. `stamp` (page facts and chunks; reads the shared evidence),
2. `catalog` (the website's own offering tree),
3. `index --tenant <namespace>` (embed, label, store; unchanged chunks are reused).

A failed knowledge step does not fail the scrape: the reports are still there and the run says what went wrong; the step can be re-run.

## 6. How the bot uses it

```
question + scope ──► for each knowledge space in scope:
                        hybrid search (vectors + keywords) inside that space
                    ──► fuse all results by rank (RRF), keep the best k
                    ──► every hit carries: project, website, role, URL, chunk, evidence ids
                    ──► answer (stage E), each point cited to its website and page
```

- **Scopes**: one website; a whole project ("client vs all competitors"); several projects (the same competitor seen in two engagements). A scope never reaches a space the user did not include.
- **Comparisons** ("what does each competitor say about secure printing?") are the same search grouped by website: the best passages per website, side by side.
- **Model consistency**: each space remembers the embedding model it was built with and is searched with that same model, so vectors are never compared across models. Results from different spaces are combined by rank, not by raw score, so spaces built with different models can still be searched together. A space whose model is not available right now (for example the local model server is off) is skipped and named in the result.
- **Permissions**: reading is open as for reports (decision of 2026-10-30, open question 1). When viewing becomes password-protected, the scope check is where it is enforced.

## 6a. A question about a website covers its competitors from every project (decided 2026-10-31)

> When any query comes, the engine takes the context of all the unique websites, checks which websites the client's website competes with, and takes all of them into consideration, regardless of the project.

- **Unique websites**: a website is the same website in every project. Two project websites are one when they share a domain, or one's domain is a subdomain of the other's (`usa.canon.com` and `canon.com`). Its name is the one used where it is the client.
- **Competitor relations from every project**: in each project the client's website and each competitor website compete with each other, in both directions. The relations of all projects are combined.
- **The competitive scope of a website**: the website itself and every website related to it as a competitor in any project. A project that lists the client as someone else's competitor counts too (HP's project naming Flynn's brings HP into Flynn's scope).
- **Each website once, freshest copy**: a website scraped in several projects is searched once, in the knowledge space built most recently; a website not yet scraped in any project is named as not searched.
- **Answers say where they come from**: each passage carries the website, the project whose scrape it came from, the page and the evidence.
- In the app this is the default of "Search all websites" ("Client and all its competitors, from every project"); "Only this project's websites" is still available. API: `GET /api/knowledge/search?q=…&website=<domain>`, and `GET /api/knowledge/websites` lists the unique websites, the projects they appear in, their competitors and which copy is freshest.

## 6b. The client's several websites, notes and tags (decided 2026-11-02)

> A client often has several websites, on one domain or under several brands. Each website can carry a note that explains it, and the engine must never treat two brands of the same client as competitors of one another when theming or analysing gaps.

- **The client's websites.** A project's websites are the client's (role `client`) or competitors' (role `competitor`). The first client website is the client's **main website**; any others are its **other websites or brands** (entered at creation under "The client's other websites or brands", or added later with "Add a website" → "Another website or brand of the client"). The main website cannot be removed or made a competitor; any other website can switch sides.
- **Never competitors of one another.** Competitor relations are only client ↔ competitor. The client's websites in one project are **sister websites**; a sister relation in any project cancels a competitor relation between the same two websites in every other project (a project entered by mistake cannot set a client against its own brand).
- **Where it applies.**
  - **Competitive scope** (search, analyst cards): the website, its sister websites, then every website any of them competes with.
  - **Theming ownership:** client pages are the client's websites together, against the strongest competitor.
  - **Analyst cards:** `role_in_this_run` is `client` for the client's websites and their sisters from any project, and `same_client_as` lists them; rule 9 tells the analyst never to count one brand as competition or a gap of another.
  - **Search results:** every passage's role is relative to the website asked about.
- **One domain, several websites.** A website's address may be a subdomain (`brand.client.com` next to `client.com`) or a section (`client.com/brand`). A section is kept only when another website of the project already has that domain; otherwise a typed page address gives just its domain. Every URL goes to the **most specific** address that covers it (`client.com/brand/x` to the brand, `client.com/brands-old` to `client.com`). Across projects, a subdomain or section is its own website when any project lists it separately from its parent domain; otherwise it joins the parent (as `usa.canon.com` joins `canon.com`).
- **Notes and tags.** Every website has a **note** (up to 2,000 characters: what it is, its market, its audience) and up to 12 **tags** (40 characters each, for example `brand`, `US`, `B2B`). They are entered when the website is created or with **Edit website**, are shown on the website's panel, and travel with the website: the websites list (`GET /api/knowledge/websites`: `note`, `tags`, `same_client`), the theme map (`websites[].note`, `tags`) and the analyst's `list_websites` tool. A note describes; it is not evidence about the website's pages.
- **API.** `POST /api/projects` takes `client_note`, `client_tags`, `client_brands[]`; `POST /api/projects/{p}/sites` takes `note`, `tags`, `role`; `POST /api/projects/{p}/sites/{s}/update` changes `name`, `note`, `tags`, `role` (password token required). Sites carry `main`, `note`, `tags`.

## 7. Existing projects

A project created before this change (one URL list, runs at project level) is converted the first time it is opened: one **client** website is created from the client name, with the domains of its URLs, and the URL list and runs move under it. Nothing is deleted.

## 8. Not in this step

The chat panel in the app (answers written by a model; the search that feeds it is built), competitors of competitors (only direct relations count), cross-project permissions beyond "read is open", automatic discovery of competitors, and competitor alignment to an industry taxonomy (RFC 0002 stage F).
