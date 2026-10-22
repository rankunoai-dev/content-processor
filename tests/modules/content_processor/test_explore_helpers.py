"""Pure helpers of the explorer: click causes, telemetry hosts, closed-root serialization."""

from __future__ import annotations

from typing import Any

import pytest

from src.modules.content_processor.contracts import InteractionPolicy
from src.modules.content_processor.explore import explorer as explorer_module
from src.modules.content_processor.explore.compose import (
    FrameCapture,
    _cdp_html,
    _cdp_text,
    _closed_hosts,
)
from src.modules.content_processor.explore.deep import deep
from src.modules.content_processor.explore.explorer import (
    StateExplorer,
    _click_cause,
    _is_telemetry,
    _note,
)
from src.modules.content_processor.explore.model import ControlRecord, ExplorationResult


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
        ("https://collection.decibelinsight.net/i/123", True),
        ("https://analytics-fe.digital-cloud.medallia.com/x", True),
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


def test_deep_rewrites_queries_and_dotted_closest() -> None:
    js = deep(
        "(id) => { const a = document.querySelector(id); "
        "const b = document.querySelectorAll('x'); "
        "return c.parentElement.closest('n') || a.closest('m'); }"
    )
    body = js.split("return null;\n  };")[-1]
    assert "__cpOne(id)" in body and "__cpAll('x')" in body
    assert "__cpClosest(c.parentElement, 'n')" in body
    assert "__cpClosest(a, 'm')" in body
    assert "document.querySelector" not in body


class _FakePage:
    async def evaluate(self, *_: object) -> None:
        return None


async def test_frames_replaced_while_being_read_are_read_again(monkeypatch) -> None:
    """A ticker swaps its iframe: pass 1 sees it detached or not laid out, pass 2 reads it."""
    passes = [
        [
            FrameCapture(
                frame_path="https://x.example/a",
                kind="cross_origin",
                error="Frame has been detached.",
            ),
            FrameCapture(frame_path="srcdoc:3", kind="srcdoc", hidden=True),
            FrameCapture(
                frame_path="https://x.example/kept",
                kind="cross_origin",
                text="Kept text.",
                html="<p>Kept text.</p>",
            ),
        ],
        [
            FrameCapture(
                frame_path="https://x.example/a",
                kind="cross_origin",
                text="Ticker A.",
                html="<p>Ticker A.</p>",
            ),
            FrameCapture(
                frame_path="srcdoc:4", kind="srcdoc", text="Ticker B.", html="<p>Ticker B.</p>"
            ),
        ],
    ]
    skips: list[set[str]] = []

    async def fake_capture(page: object, scripts: object, skip: object = ()) -> list[FrameCapture]:
        skips.append(set(skip))  # type: ignore[arg-type]
        return passes.pop(0) if passes else []

    monkeypatch.setattr(explorer_module, "capture_frames", fake_capture)
    explorer = StateExplorer(InteractionPolicy(), settle_max_s=0, settle_poll_s=0)
    result = ExplorationResult()
    added: list[ControlRecord] = []
    await explorer._frames(_FakePage(), added.append, result)
    texts = {s.frame_path: s.inner_text for s in result.states}
    assert texts == {
        "https://x.example/kept": "Kept text.",
        "https://x.example/a": "Ticker A.",
        "srcdoc:4": "Ticker B.",
    }
    assert all(c.status == "visited" for c in added), added
    assert skips[1] == {"https://x.example/kept"}  # already read, not read again


async def test_frames_that_stay_hidden_are_tracking_pixels(monkeypatch) -> None:
    pixel = FrameCapture(frame_path="https://ads.example/px", kind="cross_origin", hidden=True)
    calls: list[int] = []

    async def fake_capture(page: object, scripts: object, skip: object = ()) -> list[FrameCapture]:
        calls.append(1)
        return [pixel]

    monkeypatch.setattr(explorer_module, "capture_frames", fake_capture)
    explorer = StateExplorer(InteractionPolicy(), settle_max_s=0, settle_poll_s=0)
    added: list[ControlRecord] = []
    await explorer._frames(_FakePage(), added.append, ExplorationResult())
    assert len(calls) == 2  # one look again, then accepted as hidden
    assert [(c.status, c.reason) for c in added] == [("excluded", "hidden_frame")]


class _RoutablePage(_FakePage):
    async def route(self, *_: object) -> None:
        return None

    async def unroute(self, *_: object) -> None:
        return None


async def test_unexpected_exception_is_recorded_not_raised(monkeypatch) -> None:
    async def boom(self, page, result, unsafe, started):  # noqa: ANN001
        result.controls.append(
            ControlRecord(control_id="0", kind="tab", label="Done", status="visited")
        )
        raise ValueError("odd page")

    monkeypatch.setattr(StateExplorer, "_loop", boom)
    result = await StateExplorer(InteractionPolicy(), settle_poll_s=0).explore(_RoutablePage())
    assert result.error == "ValueError:odd page"
    assert [c.label for c in result.controls] == ["Done"]  # work done so far is kept


async def test_time_left_caps_the_exploration_budget(monkeypatch) -> None:
    seen: list[float] = []

    async def loop(self, page, result, unsafe, started):  # noqa: ANN001
        seen.append(self._max_s)

    monkeypatch.setattr(StateExplorer, "_loop", loop)
    explorer = StateExplorer(InteractionPolicy(), max_explore_s=90.0, settle_poll_s=0)
    await explorer.explore(_RoutablePage(), time_left_s=20.0)
    await explorer.explore(_RoutablePage(), time_left_s=500.0)
    await explorer.explore(_RoutablePage())
    assert seen == [20.0, 90.0, 90.0]
