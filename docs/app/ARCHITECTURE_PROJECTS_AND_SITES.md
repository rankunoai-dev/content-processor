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
| **Website** (site) | One organisation's web presence inside a project. Role `client` (exactly one) or `competitor` (any number). Has one or more **domains** (`flynns.com`, `blog.flynns.com` counts as the same site). |
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

## 7. Existing projects

A project created before this change (one URL list, runs at project level) is converted the first time it is opened: one **client** website is created from the client name, with the domains of its URLs, and the URL list and runs move under it. Nothing is deleted.

## 8. Not in this step

The chat panel in the app (the bot's screen), cross-project permissions beyond "read is open", automatic discovery of competitors, and competitor alignment to an industry taxonomy (RFC 0002 stage F).
