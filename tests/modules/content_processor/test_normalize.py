from __future__ import annotations

import pytest

from src.modules.content_processor.scope.normalize import InvalidInputUrl, normalize_url


def test_schedule_key_strips_tracking_and_sorts_query():
    n = normalize_url("HTTPS://WWW.Example.COM:443/Path/?b=2&utm_source=x&a=1&gclid=abc")
    assert n.schedule_key == "https://www.example.com/Path/?a=1&b=2"
    assert sorted(n.dropped_params) == ["gclid", "utm_source"]
    assert n.host == "www.example.com"


def test_schedule_key_preserves_path_case_and_trailing_slash():
    a = normalize_url("https://example.com/Docs/")
    b = normalize_url("https://example.com/docs")
    assert a.schedule_key != b.schedule_key
    assert a.near_duplicate_key == b.near_duplicate_key


def test_near_duplicate_key_merges_www_scheme_and_index():
    a = normalize_url("http://www.example.com/a/index.html")
    b = normalize_url("https://example.com/a/")
    assert a.near_duplicate_key == b.near_duplicate_key


def test_fragment_route_is_kept_as_state_not_identity():
    n = normalize_url("https://example.com/app#/pricing")
    assert n.schedule_key == "https://example.com/app"
    assert n.fragment_route == "/pricing"
    plain = normalize_url("https://example.com/app#section-2")
    assert plain.fragment_route is None


def test_idn_host_is_punycoded():
    n = normalize_url("https://bücher.example/")
    assert n.host == "xn--bcher-kva.example"


def test_percent_encoding_is_normalized():
    a = normalize_url("https://example.com/a%7Eb")
    b = normalize_url("https://example.com/a~b")
    assert a.schedule_key == b.schedule_key


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("mailto:a@b.c", "unsupported_scheme:mailto"),
        ("javascript:alert(1)", "unsupported_scheme:javascript"),
        ("ftp://example.com/x", "unsupported_scheme:ftp"),
        ("example.com/path", "unsupported_scheme:none"),
        ("https:///path", "missing_host"),
        ("https://user:pw@example.com/", "credentials_in_url"),
        ("https://example.com/a b", "contains_whitespace"),
        ("", "empty"),
        ("https://example.com/" + "x" * 2100, "too_long"),
        ("https://example.com:99999/", "invalid_port"),
    ],
)
def test_invalid_inputs_are_rejected_with_reason(raw: str, reason: str):
    with pytest.raises(InvalidInputUrl) as info:
        normalize_url(raw)
    assert info.value.reason == reason


def test_seed_near_duplicates_group_but_schedule_separately():
    a = normalize_url(
        "https://www.johnhancock.com/about-us/news/john-hancock/2026/06/"
        "john-hancock-governor-healey-and-mayor-wu-call-on-community"
    )
    b = normalize_url(
        "https://www.johnhancock.com/about-us/news/john-hancock/2026/06/"
        "john-hancock--governor-healey--and-mayor-wu-call-on-community"
    )
    # Different slugs are different resources; grouping happens downstream (case 91).
    assert a.schedule_key != b.schedule_key
    assert a.host == b.host == "www.johnhancock.com"
