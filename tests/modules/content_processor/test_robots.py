from __future__ import annotations

from src.modules.content_processor.enums import RobotsPolicy
from src.modules.content_processor.governor.robots import evaluate_robots

ROBOTS = (
    "User-agent: *\nDisallow: /private/\nCrawl-delay: 2\n\n"
    "User-agent: RankunoContentBot\nDisallow: /bot-only/\n"
)


def _eval(url: str, policy: RobotsPolicy = RobotsPolicy.RESPECT, **kw):
    base = {"robots_body": ROBOTS, "robots_status": 200, "user_agent_token": "OtherBot"}
    base.update(kw)
    return evaluate_robots(url=url, policy=policy, **base)


def test_allowed_path():
    v = _eval("https://x.example/public")
    assert v.allowed and v.directive_allows and v.source == "parsed"


def test_disallowed_under_respect():
    v = _eval("https://x.example/private/a")
    assert not v.allowed and v.reason == "robots_disallow:parsed"


def test_specific_group_replaces_the_wildcard_group():
    ours = "RankunoContentBot"
    assert not _eval("https://x.example/bot-only/x", user_agent_token=ours).allowed
    assert _eval("https://x.example/private/x", user_agent_token=ours).allowed
    assert _eval("https://x.example/bot-only/x").allowed
    assert _eval("https://x.example/private/x", user_agent_token=ours).crawl_delay_s is None


def test_informational_records_but_allows():
    v = _eval("https://x.example/private/a", policy=RobotsPolicy.INFORMATIONAL)
    assert v.allowed and not v.directive_allows
    assert v.reason == "informational:directive_disallows"


def test_exceptions_only_for_listed_prefixes():
    allowed = _eval(
        "https://x.example/private/docs/1",
        policy=RobotsPolicy.RESPECT_WITH_EXCEPTIONS,
        exceptions=["/private/docs/"],
    )
    assert allowed.allowed and allowed.reason == "allowed_by_exception"
    denied = _eval(
        "https://x.example/private/other",
        policy=RobotsPolicy.RESPECT_WITH_EXCEPTIONS,
        exceptions=["/private/docs/"],
    )
    assert not denied.allowed


def test_rfc9309_availability_rules():
    assert _eval("https://x.example/private/a", robots_body=None, robots_status=404).allowed
    unreachable = _eval("https://x.example/public", robots_body=None, robots_status=None)
    assert not unreachable.allowed and unreachable.source == "unreachable_disallow"
    server_error = _eval("https://x.example/public", robots_body=None, robots_status=503)
    assert not server_error.allowed


def test_crawl_delay_is_reported():
    v = _eval("https://x.example/public")
    assert v.crawl_delay_s == 2.0
