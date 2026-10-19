"""Pure helpers of the explorer: click causes, telemetry hosts, closed-root serialization."""

from __future__ import annotations

from typing import Any

import pytest

from src.modules.content_processor.explore.compose import _cdp_html, _cdp_text, _closed_hosts
from src.modules.content_processor.explore.explorer import _click_cause, _is_telemetry, _note
from src.modules.content_processor.explore.model import ControlRecord


@pytest.mark.parametrize(
    ("message", "cause"),
    [
        ("Timeout 3000ms exceeded. <div> intercepts pointer events", "intercepted"),
        ("Timeout 3000ms exceeded. element is not visible", "not_visible"),
        ("Element is outside of the viewport", "outside_viewport"),
        ("Element is detached from the DOM", "detached"),
        ("Timeout 3000ms exceeded.", "timeout"),
        ("something else", "error"),
    ],
)
def test_click_cause(message: str, cause: str) -> None:
    assert _click_cause(Exception(message)) == cause


@pytest.mark.parametrize(
    ("url", "telemetry"),
    [
        ("https://privacyportal-ca.onetrust.com/request/v1/consentreceipts", True),
        ("https://bam.nr-data.net/jserrors/1/x", True),
        ("https://www.google-analytics.com/g/collect", True),
        ("https://edge.adobedc.net/ee/v1/privacy/set-consent", True),
        ("https://www.johnhancock.com/api/forms", False),
        ("https://notonetrust.com/x", False),
    ],
)
def test_telemetry_hosts(url: str, telemetry: bool) -> None:
    assert _is_telemetry(url) is telemetry


def test_note_keeps_the_original_reason() -> None:
    rec = ControlRecord(control_id="1", kind="dialog", status="unvisited", reason="click_failed")
    _note(rec, "dialog_left_open")
    assert rec.reason == "click_failed;dialog_left_open"
    fresh = ControlRecord(control_id="2", kind="dialog")
    _note(fresh, "no_new_visible_state")
    assert fresh.reason == "no_new_visible_state"


def _el(tag: str, *children: dict[str, Any], **attrs: str) -> dict[str, Any]:
    flat = [x for kv in attrs.items() for x in kv]
    return {"nodeType": 1, "localName": tag, "attributes": flat, "children": list(children)}


def _txt(value: str) -> dict[str, Any]:
    return {"nodeType": 3, "nodeValue": value}


def test_closed_root_is_serialized_with_slots_filled_from_the_host() -> None:
    root = {
        "nodeType": 11,
        "shadowRootType": "closed",
        "children": [
            _el("p", _txt("Closed <text> & more")),
            _el("slot", _txt("fallback"), name="price"),
            _el("slot", _txt("default fallback")),
            _el("style", _txt("p{color:red}")),
        ],
    }
    host = _el("x-card", _el("span", _txt("$12"), slot="price"), backendNodeId="7")
    host["shadowRoots"] = [root]
    host["backendNodeId"] = 7
    doc = _el("html", _el("body", host))

    found = list(_closed_hosts(doc))
    assert len(found) == 1 and found[0][0]["backendNodeId"] == 7

    html = _cdp_html(root, host["children"])
    assert "<p>Closed &lt;text&gt; &amp; more</p>" in html
    assert html.count('<span slot="price">$12</span>') == 1  # named slot, filled once
    assert "default fallback" in html  # nothing unnamed in the host: fallback shows
    assert ">fallback" not in html
    texts: list[str] = []
    _cdp_text(root, texts)
    assert texts == ["Closed <text> & more", "fallback", "default fallback"]


def test_open_root_inside_tree_is_walked_for_closed_roots() -> None:
    inner_closed = {"nodeType": 11, "shadowRootType": "closed", "children": [_txt("deep")]}
    inner_host = _el("y-box")
    inner_host["shadowRoots"] = [inner_closed]
    open_root = {"nodeType": 11, "shadowRootType": "open", "children": [inner_host]}
    outer = _el("x-outer")
    outer["shadowRoots"] = [open_root]
    hosts = [h for h, _ in _closed_hosts(_el("body", outer))]
    assert hosts == [inner_host]
