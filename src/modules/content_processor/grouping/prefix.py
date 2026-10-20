"""Path-family keys for URL grouping.

The family is the first path segment, with a leading slash: `/services/tax`
and `/services/estate` share `/services`, while `/services-archive/x` gets
`/services-archive`. Segment boundaries matter: a plain string-prefix match
would merge the two.
"""

from __future__ import annotations

from urllib.parse import urlsplit

__all__ = ["path_family"]


def path_family(url: str) -> str:
    """Return `/<first-segment>` for a URL, or `/` for the site root."""
    segments = [s for s in urlsplit(url).path.split("/") if s]
    return f"/{segments[0]}" if segments else "/"
