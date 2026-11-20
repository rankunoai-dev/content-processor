"""Competitor lite scan: understand a big competitor website without scraping all of it.

A full scrape of a 5,000-page competitor costs hours of polite requests. A lite scan
reads the website's sitemaps (the list of every page it publishes, a few requests),
groups the addresses by section, and scrapes a capped sample (`CP_LITE_SCAN_MAX_PAGES`,
150 by default) with plain downloads only: no browser, no documents. The sample is the
pages that matter most for a comparison: those a person pinned, those that rank in the
search data, the core pages (pricing, products, solutions, the home page and one level
from it), then a few pages from every section of the website.

The rest of the sitemap is kept as the **inventory**: the theme map counts those pages
as "inferred" from their addresses, and every gap finding says what its competitor
figures are based on ("150 of about 5,000 pages").
"""
