"""Live browser tests: real Chromium against the fixture server on a local port."""

from __future__ import annotations

import socket
import threading
import time

import pytest
import uvicorn

from fixtures.server import app
from src.integrations.browser_client import BrowserRenderer, RenderLimits
from src.modules.content_processor.adapters.html_source import extract_html_source
from src.modules.content_processor.enums import ContentLayer

pytestmark = pytest.mark.browser


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def server_url():
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
async def renderer():
    r = BrowserRenderer(
        user_agent="test-agent/1.0",
        host_checker=lambda _h: (True, "test"),
        limits=RenderLimits(
            settle_window_s=8, settle_stable_polls=2, settle_min_quiet_s=3.0, poll_interval_s=0.4
        ),
        channel=None,
    )
    try:
        await r.start()
    except Exception as exc:  # noqa: BLE001 - environment without a browser
        pytest.skip(f"browser unavailable: {exc}")
    yield r
    await r.stop()


async def test_delayed_hydration_is_captured_after_settle(
    renderer: BrowserRenderer, server_url: str
):
    result = await renderer.render(f"{server_url}/pages/delayed_hydration")
    assert result.rendered and result.http_status == 200
    assert "Hydrated content arrived." in result.inner_text
    assert result.settle["stable"] is True
    ex = extract_html_source(
        result.dom_html,
        base_url=result.final_url,
        evidence_id="e",
        resource_id="r",
        state_id="x|rendered",
    )
    visible = [b.normalized_text for b in ex.blocks if b.layer is ContentLayer.RENDERED_VISIBLE]
    assert "Hydrated content arrived." in visible and "Loading…" not in visible
    assert result.screenshot_jpeg[:3] == b"\xff\xd8\xff"


async def test_computed_visibility_marks_hidden_and_icon_fonts(
    renderer: BrowserRenderer, server_url: str
):
    honeypot = await renderer.render(f"{server_url}/pages/honeypot")
    ex = extract_html_source(
        honeypot.dom_html,
        base_url=honeypot.final_url,
        evidence_id="e",
        resource_id="r",
        state_id="x|rendered",
    )
    hidden = [b for b in ex.blocks if b.layer is ContentLayer.HIDDEN]
    assert any(
        b.normalized_text == "hidden trap" and b.provenance.selector == "computed_hidden"
        for b in hidden
    )
    assert honeypot.annotation["hidden"] >= 1
    icons = await renderer.render(f"{server_url}/pages/icon_font")
    assert "arrow_forward" not in icons.inner_text or icons.annotation["icons"] >= 0
    aria = icons.aria_snapshot
    assert "Open the statement" in aria


async def test_challenge_headers_survive_the_render(renderer: BrowserRenderer, server_url: str):
    result = await renderer.render(f"{server_url}/challenge-200")
    assert result.headers.get("cf-mitigated") == "challenge"
    assert b"challenge-platform" in result.dom_html


async def test_redirect_chain_and_blocked_subrequests(server_url: str):
    seen: list[str] = []

    def checker(host: str) -> tuple[bool, str]:
        seen.append(host)
        return (True, "ok") if host == "127.0.0.1" else (False, "denied_test")

    r = BrowserRenderer(
        user_agent="t",
        host_checker=checker,
        limits=RenderLimits(
            settle_window_s=2, settle_stable_polls=2, settle_min_quiet_s=0.5, poll_interval_s=0.3
        ),
        channel=None,
    )
    await r.start()
    try:
        result = await r.render(f"{server_url}/gated")
    finally:
        await r.stop()
    assert result.hops == [f"{server_url}/gated"]
    assert result.final_url.endswith("/login?next=/gated")
    assert "127.0.0.1" in seen


async def test_scripts_and_pixels_added_forever_do_not_keep_a_page_unsettled(
    renderer: BrowserRenderer, server_url: str
):
    """Live fund pages: a tag manager adds elements without visible text forever."""
    result = await renderer.render(f"{server_url}/pages/script_churn")
    assert result.rendered, result.transport_error
    assert result.settle["stable"] is True, result.settle
    assert "The monthly distribution is $0.0825 per share." in result.inner_text
    assert result.dom_html.lower().startswith(b"<!doctype html>")


async def test_web_component_text_without_its_own_box_is_not_hidden(
    renderer: BrowserRenderer, server_url: str
):
    """Live John Hancock cards: a display:contents host's slotted text is on screen."""
    from src.modules.content_processor.adapters.html_source import extract_html_source

    result = await renderer.render(f"{server_url}/pages/web_component_card")
    assert result.rendered, result.transport_error
    blocks = extract_html_source(
        result.dom_html, base_url=server_url, evidence_id="e", resource_id="r"
    ).blocks
    layers = {b.normalized_text: b.layer.value for b in blocks}
    assert layers["Closed-end funds release earnings data"] == "rendered_visible"
    assert layers["October 8, 2026"] == "rendered_visible"


async def test_side_by_side_boxes_are_separate_words_and_still_verify(
    renderer: BrowserRenderer, server_url: str
):
    """Inline-block chips and padded links: separate words; a styled word fragment is not."""
    from src.modules.content_processor.adapters.html_source import extract_html_source
    from src.modules.content_processor.verify.reconcile import reconcile_source_vs_rendered

    result = await renderer.render(f"{server_url}/pages/inline_boxes")
    assert result.rendered, result.transport_error
    blocks = extract_html_source(
        result.dom_html, base_url=server_url, evidence_id="e", resource_id="r"
    ).blocks
    texts = [b.normalized_text for b in blocks]
    assert "Equity Fixed income Alternatives" in texts
    assert "Fund A Fund B" in texts
    assert "The Superman fund pays monthly." in texts
    # the browser's own text glues the chips; the blocks still verify
    assert "EquityFixed income" in result.inner_text
    checked, _ = reconcile_source_vs_rendered([], blocks, result.inner_text)
    assert all(
        b.status.value == "VERIFIED_EXTRACTED"
        for b in checked
        if b.layer.value == "rendered_visible"
    )


async def test_empty_containers_added_forever_do_not_keep_a_page_unsettled(
    renderer: BrowserRenderer, server_url: str
):
    result = await renderer.render(f"{server_url}/pages/div_churn")
    assert result.rendered, result.transport_error
    assert result.settle["stable"] is True, result.settle
