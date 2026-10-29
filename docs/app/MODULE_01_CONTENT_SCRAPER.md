# App, Module 1: Content Scraper (projects)

Status: requirements captured 2026-10-30 from the product owner (Gaurav Doshi); first build in progress. More requirements for this module will follow; add them here as they arrive.

## 1. The app

The content processor becomes one app with several modules. **Module 1 is the Content Scraper**: it turns a list of URLs into extracted, verified content and downloadable reports. Later modules (to be specified) build on its output (knowledge base, inventory, competitor comparison, and so on).

## 2. Requirements as given (2026-10-30)

1. In the Content Scraper module a person **creates a project**. To create it they enter **their name**, the **client name** and a **password**.
2. **Only the person with the credentials has write access** to that project.
3. The person **puts down all the URLs** of the project (the URL inventory).
4. There is an **import from the device** option for the URL list.
5. Then **content scraping starts**.
6. **Multiple reports can be downloaded** from the project.

## 3. Design decisions for the first build (defaults; change on request)

| Topic | Decision | Why |
|---|---|---|
| Project fields | Project name, owner name, client name, password; created date | A project needs a name of its own (one client can have several projects) |
| Who can see a project | Anyone using the app can **view** projects and download their reports; **writing** needs the project password | The requirement restricts write access only. If viewing must also be private, it is a one-line change of policy (open question 1) |
| What "write" covers | Adding, importing and removing URLs; starting and cancelling scraping; renaming; deleting the project | Everything that changes the project |
| Password | At least 8 characters; stored only as a salted scrypt hash, never in plain text; wrong attempts are slowed down | Standard practice |
| Unlocking | Entering the password unlocks the project in that browser tab for 8 hours (a signed token, kept in memory, sent with every write) | No accounts needed yet |
| Forgotten password | Not recoverable in the app; an administrator can reset it on the server | No accounts or email yet (open question 2) |
| URL entry | Paste one URL per line; or import a file from the device: `.txt`, `.csv` or `.xlsx` (URLs found in any column) | Covers how lists usually arrive |
| URL checks | Only `http`/`https`; addresses are normalised, duplicates removed; the import says how many were added, already present or rejected, and why | No silent loss |
| Scraping | One button starts a run over the project's URLs. Runs go in batches of 25; a batch that leaves pages unfinished is re-run for those pages only (up to 3 tries). The URL history is used, so unchanged pages are reused and only new or changed pages are scraped in full | What worked for the 303-URL John Hancock run, made automatic |
| Cost and access | Free tiers only (up to the real browser, T3), $0. Paid access stays off unless approved per website, as everywhere else | Existing safety rules |
| One run at a time | Runs queue; progress shows batches and pages done, and the results so far; a run can be cancelled | One machine, one browser pool |
| Reports | Each finished run keeps its own folder with every report (project report, content, review queue, overlap, contradictions, explorer); each file can be downloaded, or all of them as one zip | "Multiple reports can be downloaded" |
| Storage | `app_data/projects/<project id>/` (project, URLs, runs and reports), outside git | Scraped data never goes into the repository |
| Where it runs | Same server as the analyst web app, on this machine (127.0.0.1) | A team deployment (network, HTTPS, accounts) is a later step (open question 3) |

## 4. Open questions for the product owner

1. Should other people be able to **view** a project without the password, or should the password protect viewing too?
2. Do you want **user accounts** (login once, see your projects) instead of a password per project, and a way to reset a forgotten password?
3. Where will the app run for the team (one shared server, cloud), and who may use it?
4. Which reports matter most for this module's download page, and in what order?
5. Should a project's URL list be editable after a run (re-scrape everything, or only new URLs)?

## 5. Build log

- 2026-10-30: requirements captured; build of projects, URL inventory and import, password-protected writes, background scraping and report downloads started on `feat/app-module1-projects`.
