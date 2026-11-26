# Module: Analytics (GA4: what happens after the click)

Status: **built 2026-11-26** on `feat/ga4-analytics` (`src/modules/analytics/`). The rest of
the app sees before the click (Semrush rankings, Search Console impressions and CTR) and the
page itself (scrape, themes, links, Ahrefs). GA4 adds what visitors do after they land:
sessions, engagement, key events (conversions), revenue, the channel split and visits from
AI assistants.

## Two ways in

| Way | Cost | Needs |
|---|---|---|
| **CSV import** | $0, no sign-in | GA4: Reports, Engagement, Landing page; add *Session default channel group* (and, for AI referrals, a second export with *Session source*) as a secondary dimension; Share, Download file, Download CSV. Upload in the Research tab or `analytics import --project <id> --file ga4.csv` |
| **Data API v1 pull** | $0 (GA4 API quotas, no units) | `CP_GA4_ENABLED=true`, `GA4_CREDENTIALS_FILE` (a service account key file), the project's numeric GA4 property id (`analytics settings --project <id> --property 123456789` or the tab), and the service account's email added to the property as a **Viewer** |

A pull keeps two periods of `CP_GA4_PERIOD_DAYS` (28) ending yesterday: the latest and the
one before, so decays show at once. It asks `runReport` for landing page x channel (sessions,
engaged sessions, engagement rate, average session duration, key events, total revenue) and
landing page x source (sessions from AI assistants only), at most `CP_GA4_MAX_REQUESTS` (20)
requests per pull, paced `CP_GA4_MIN_INTERVAL_MS`, 429 and 503 retried twice.

## Safety

| Rule | How |
|---|---|
| Off by default | Pulls refuse until switched on, with a key file and a property id; the CSV import always works. |
| Where the key goes | Signed tokens go only to `https://oauth2.googleapis.com/token` and reports to `https://analyticsdata.googleapis.com/`, or to this computer (the stand-in); anything else is refused before a call (`ga4.address_refusal`). |
| No new dependency | The service account's JWT is signed with `cryptography` (already installed); the key file is never logged or shown. |
| Read only | Scope `analytics.readonly`. |
| Dummy data labelled | A pull from the stand-in carries a "DUMMY DATA" note. |

## What it is used for

- **Page report** (`research/pages.py`): per page `ga_sessions`, `ga_organic_sessions`,
  `ga_engagement_rate`, `ga_avg_engagement_s`, `ga_conversions`, `ga_revenue`,
  `ga_ai_sessions`. A page that ranks for no keyword but converts, or has 50+ sessions from
  other channels, is listed in `kept_by_ga4` instead of "ranks for nothing" (never pruned).
  A cannibalisation pair whose competing page converts at least twice the owner's rate (5+
  key events) gets an `owner_check`: confirm the owner before consolidating.
- **Rollups** (`analytics/insights.py`): sessions, organic sessions, key events, revenue and
  conversion rate per theme of the theme map (with its search demand) and per funnel stage.
- **Gaps** (`gaps/build.py`): `leaky_page` (100+ visits from search, under 40% engaged or no
  key event while the site converts), `traffic_decay` (organic sessions or key events down 30%+
  against the period before), `cta_no_conversions` (a strong call to action or a form, 100+
  sessions, no key event); findings of a theme converting 1.5x the site's rate get +6.
- **Monitoring**: every check adds `client_traffic_decay` and `client_conversion_drop`
  alerts from the decays; the analytics report lists pages changed between scrapes with
  their sessions and key events before and after.
- **Claude Desktop**: `get_analytics` (summary, themes, funnel, pages, leaky, decays, ctas,
  ai_referrals, after_edits). Cards `content-refresh-queue`, `executive-briefing`,
  `funnel-coverage` and `keyword-cannibalisation` use it when the project has GA4 data.
- **App**: the Research tab's Analytics (GA4) card; the page report's GA4 column.
- **Storage**: `<app data>/analytics/<project>/ga4-*.json`, `latest.json`, `settings.json`,
  `analytics.xlsx` (landing pages, by theme, by stage, leaky pages, decays, CTAs without
  conversions, AI referrals, after edits).

## Trying it without GA4 (stand-in)

```
python -m src.modules.content_processor.cli analytics-standin make --project <id> --out dummy
python -m src.modules.content_processor.cli analytics-standin serve --out dummy --port 8792
```

`make` writes two GA4 exports (latest and previous period) for the client's theme-map pages
and a throwaway service-account key; `serve` answers `/token` and `runReport` on 127.0.0.1.
For a pull set `CP_GA4_ENABLED=true`, `GA4_CREDENTIALS_FILE=dummy/dummy-service-account.json`,
`CP_GA4_BASE_URL=http://127.0.0.1:8792/`, `CP_GA4_TOKEN_URL=http://127.0.0.1:8792/token`.
Use a copy of the project.

## Connecting the real GA4 Data API

1. In Google Cloud, create a project (or use one), enable the **Google Analytics Data API**,
   create a **service account** and a JSON key for it.
2. In GA4, Admin, Property access management, add the service account's email as a
   **Viewer**.
3. `.env` (never in chat): `GA4_CREDENTIALS_FILE=<path to the key file>`,
   `CP_GA4_ENABLED=true`. Leave the base and token URLs unset.
4. Set the project's property id (the number in GA4, Admin, Property settings; not the
   `G-...` measurement id), then pull from the Research tab or `analytics pull`.

Scheduled pulls with Monitoring are not built yet; a pull runs when asked.
