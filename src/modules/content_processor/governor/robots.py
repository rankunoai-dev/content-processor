"""robots.txt evaluation (case 84, RFC 9309 semantics, playbook section 2 T1).

Policy decides whether a disallow is enforced (`respect`), enforced with an
allowlist of path prefixes (`respect_with_exceptions`), or only recorded
(`informational`). Crawl-delay is honoured in every mode.
"""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from src.core.schemas import StrictModel
from src.modules.content_processor.enums import RobotsPolicy

__all__ = ["MAX_CRAWL_DELAY_S", "RobotsVerdict", "crawl_delay_for", "evaluate_robots"]

# A host cannot stall the run indefinitely: longer requests are honoured at this cap.
MAX_CRAWL_DELAY_S = 30.0


class RobotsVerdict(StrictModel):
    """Outcome of applying robots policy to one URL."""

    allowed: bool
    directive_allows: bool
    source: Literal["parsed", "unavailable_allow", "unreachable_disallow", "no_robots"]
    crawl_delay_s: float | None = None
    reason: str


def evaluate_robots(
    *,
    url: str,
    user_agent_token: str,
    policy: RobotsPolicy,
    robots_body: str | None,
    robots_status: int | None,
    exceptions: list[str] | None = None,
) -> RobotsVerdict:
    """Apply RFC 9309 availability rules, then the engagement's policy.

    Args:
        url: The URL about to be fetched.
        user_agent_token: Product token used for group matching (e.g. "RankunoContentBot").
        policy: Engagement robots policy.
        robots_body: Text of robots.txt if it was fetched with a 2xx.
        robots_status: HTTP status of the robots fetch; None means unreachable.
        exceptions: Path prefixes exempt under `respect_with_exceptions`.
    """
    path = urlsplit(url).path or "/"
    crawl_delay: float | None = None

    if robots_status is None or robots_status >= 500:
        directive_allows = False
        source: Literal["parsed", "unavailable_allow", "unreachable_disallow", "no_robots"] = (
            "unreachable_disallow"
        )
    elif robots_status >= 400 or robots_body is None:
        directive_allows = True
        source = "unavailable_allow"
    else:
        parser = RobotFileParser()
        parser.parse(robots_body.splitlines())
        directive_allows = parser.can_fetch(user_agent_token, url)
        delay = parser.crawl_delay(user_agent_token)
        crawl_delay = float(delay) if delay is not None else None
        source = "parsed"

    if policy is RobotsPolicy.INFORMATIONAL:
        return RobotsVerdict(
            allowed=True,
            directive_allows=directive_allows,
            source=source,
            crawl_delay_s=crawl_delay,
            reason="informational" if directive_allows else "informational:directive_disallows",
        )
    if directive_allows:
        return RobotsVerdict(
            allowed=True,
            directive_allows=True,
            source=source,
            crawl_delay_s=crawl_delay,
            reason="allowed",
        )
    if policy is RobotsPolicy.RESPECT_WITH_EXCEPTIONS and any(
        path.startswith(prefix) for prefix in (exceptions or [])
    ):
        return RobotsVerdict(
            allowed=True,
            directive_allows=False,
            source=source,
            crawl_delay_s=crawl_delay,
            reason="allowed_by_exception",
        )
    return RobotsVerdict(
        allowed=False,
        directive_allows=False,
        source=source,
        crawl_delay_s=crawl_delay,
        reason=f"robots_disallow:{source}",
    )


def crawl_delay_for(robots_body: str | None, user_agent_token: str) -> float | None:
    """Crawl-delay from the group matching `user_agent_token` (else `*`), capped.

    The same parser `evaluate_robots` uses, so the delay a ledger stores is the
    one the policy decision saw. None when there is no body or no directive.
    """
    if not robots_body:
        return None
    parser = RobotFileParser()
    parser.parse(robots_body.splitlines())
    delay = parser.crawl_delay(user_agent_token)
    if delay is None:
        return None
    return min(max(float(delay), 0.0), MAX_CRAWL_DELAY_S)
