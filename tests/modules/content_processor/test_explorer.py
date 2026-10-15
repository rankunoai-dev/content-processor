"""Live state-explorer tests: real Chromium against the fixture server (B-1, B-2, B-3)."""

from __future__ import annotations

import socket
import threading
import time
from typing import Any

import pytest
import uvicorn

from fixtures.server import app
from src.integrations.browser_client import BrowserRenderer, RenderLimits
from src.modules.content_processor.contracts import InteractionPolicy
from src.modules.content_processor.explore import ExplorationResult, StateExplorer

pytestmark = pytest.mark.browser

CONTROLS_ONLY = ["expand_disclosure", "select_tab"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def server_url():
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
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
            settle_window_s=6, settle_stable_polls=2, settle_min_quiet_s=0.6, poll_interval_s=0.3
        ),
        channel=None,
    )
    try:
        await r.start()
    except Exception as exc:  # noqa: BLE001 - environment without a browser
        pytest.skip(f"browser unavailable: {exc}")
    yield r
    await r.stop()


async def _explore(
    renderer: BrowserRenderer, url: str, policy: InteractionPolicy
) -> tuple[Any, ExplorationResult]:
    explorer = StateExplorer(policy, settle_max_s=1.5)
    holder: dict[str, ExplorationResult] = {}

    async def hook(page: Any) -> None:
        holder["r"] = await explorer.explore(page)

    result = await renderer.render(url, on_page=hook)
    assert result.rendered, result.transport_error
    return result, holder["r"]


def _by_label(ex: ExplorationResult) -> dict[str, tuple[str, str | None]]:
    return {c.label: (c.status, c.reason) for c in ex.controls}


async def test_faq_disclosures_close_discovery(renderer: BrowserRenderer, server_url: str):
    base, ex = await _explore(
        renderer, f"{server_url}/pages/details_collapsed", InteractionPolicy(allowed=CONTROLS_ONLY)
    )
    assert "Collapsed but published answer." not in base.inner_text
    assert ex.controls_closed and ex.discovery_closed, ex.controls
    kinds = sorted(c.kind for c in ex.controls)
    assert kinds == ["disclosure", "find_reveal"]
    revealed = " ".join(s.inner_text for s in ex.states)
    assert "Collapsed but published answer." in revealed
    assert "Find-in-page revealable note." in revealed
    assert ex.unsafe_requests == []


async def test_all_tabs_are_visited(renderer: BrowserRenderer, server_url: str):
    base, ex = await _explore(
        renderer, f"{server_url}/pages/tabs", InteractionPolicy(allowed=CONTROLS_ONLY)
    )
    assert "Annual plan costs" not in base.inner_text
    status = _by_label(ex)
    assert status["Monthly"] == ("initially_open", "tab_initially_selected")
    assert status["Annual"][0] == "visited" and status["Enterprise"][0] == "visited"
    assert len(ex.states) == 2 and ex.discovery_closed
    assert "Annual plan costs $120 per user" in ex.states[0].inner_text
    assert "Enterprise pricing requires" in ex.states[1].inner_text
    assert b'data-cp-hidden="1"' in ex.states[1].dom_html  # inactive panels re-annotated


async def test_action_budget_leaves_tab_unvisited(renderer: BrowserRenderer, server_url: str):
    _, ex = await _explore(
        renderer,
        f"{server_url}/pages/tabs",
        InteractionPolicy(allowed=CONTROLS_ONLY, max_actions_per_url=1),
    )
    assert ex.stop_reason == "action_budget_exhausted"
    assert [(c.label, c.reason) for c in ex.unvisited] == [
        ("Enterprise", "action_budget_exhausted")
    ]
    assert not ex.controls_closed


async def test_policy_without_tabs_leaves_tabs_unvisited(
    renderer: BrowserRenderer, server_url: str
):
    _, ex = await _explore(
        renderer, f"{server_url}/pages/tabs", InteractionPolicy(allowed=["expand_disclosure"])
    )
    reasons = {c.label: c.reason for c in ex.unvisited}
    assert reasons == {
        "Annual": "policy_disallows:select_tab",
        "Enterprise": "policy_disallows:select_tab",
    }
    assert ex.actions_used == 0 and ex.states == []


async def test_accordion_opens_and_nav_menu_is_excluded(renderer: BrowserRenderer, server_url: str):
    _, ex = await _explore(
        renderer, f"{server_url}/pages/accordion", InteractionPolicy(allowed=CONTROLS_ONLY)
    )
    status = _by_label(ex)
    assert status["Products"] == ("excluded", "site_navigation_menu")
    assert status["How long does delivery take?"][0] == "visited"
    assert status["Can I return an item?"][0] == "visited"
    assert ex.discovery_closed
    final = ex.states[-1].inner_text
    assert "Delivery takes 3 to 5 working days." in final
    assert "Returns are accepted within 30 days" in final


async def test_side_effect_click_is_blocked_and_reported(
    renderer: BrowserRenderer, server_url: str
):
    _, ex = await _explore(
        renderer,
        f"{server_url}/pages/accordion_side_effect",
        InteractionPolicy(allowed=CONTROLS_ONLY),
    )
    status = _by_label(ex)
    assert status["Opening hours"][0] == "visited"
    phone = status["Show phone number"]
    assert phone[0] == "unvisited" and (phone[1] or "").startswith("unsafe_side_effect:POST")
    assert any(r.startswith("POST ") for r in ex.unsafe_requests)
    assert not ex.controls_closed


async def test_default_policy_reports_unimplemented_scroll(
    renderer: BrowserRenderer, server_url: str
):
    _, ex = await _explore(renderer, f"{server_url}/pages/static_ok", InteractionPolicy())
    assert ex.controls == [] and ex.controls_closed
    assert ex.unexplored_action_kinds == ["scroll"]
    assert not ex.discovery_closed
