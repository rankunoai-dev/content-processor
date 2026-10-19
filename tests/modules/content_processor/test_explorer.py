"""Live state-explorer tests: real Chromium against the fixture server (B-1 to B-12)."""

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
# A scope that forbids dialogs and paging (the defaults allow both since CR-009).
NO_DIALOGS_OR_PAGING = ["scroll", "expand_disclosure", "select_tab"]


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


async def test_default_policy_scrolls_short_page_and_closes_discovery(
    renderer: BrowserRenderer, server_url: str
):
    _, ex = await _explore(renderer, f"{server_url}/pages/static_ok", InteractionPolicy())
    assert ex.unexplored_action_kinds == []
    assert [(c.kind, c.status, c.reason) for c in ex.controls] == [
        ("scroll_surface", "visited", "reached_end")
    ]
    assert ex.discovery_closed


async def test_lazy_section_is_loaded_by_scrolling(renderer: BrowserRenderer, server_url: str):
    base, ex = await _explore(renderer, f"{server_url}/pages/lazy_sections", InteractionPolicy())
    assert "fee is waived for students" not in base.inner_text
    window = next(c for c in ex.controls if c.control_id == "scroll:window")
    assert window.status == "visited" and window.reason == "reached_end:content_grew"
    assert ex.discovery_closed
    assert any("fee is waived for students" in s.inner_text for s in ex.states)
    # captured from the top: nothing above the fold is mislabelled hidden
    scroll_state = next(s for s in ex.states if s.kind == "scroll_surface")
    assert b"Intro paragraph visible on load." in scroll_state.dom_html
    assert ex.scroll_surfaces[0]["grew"] is True


async def test_infinite_feed_stops_at_budget_and_stays_open(
    renderer: BrowserRenderer, server_url: str
):
    _, ex = await _explore(
        renderer,
        f"{server_url}/pages/infinite_scroll",
        InteractionPolicy(max_scroll_passes=4),
    )
    window = next(c for c in ex.controls if c.control_id == "scroll:window")
    assert (window.status, window.reason) == ("unvisited", "scroll_budget_exhausted")
    assert not ex.discovery_closed
    assert ex.scroll_surfaces[0]["passes"] == 4


async def test_nested_scroller_is_its_own_surface(renderer: BrowserRenderer, server_url: str):
    base, ex = await _explore(renderer, f"{server_url}/pages/nested_scroller", InteractionPolicy())
    nested = [c for c in ex.controls if c.control_id.startswith("scroll:nested:")]
    assert len(nested) == 1 and nested[0].label == "terms box"
    assert nested[0].status == "visited" and nested[0].reason.startswith("reached_end")
    assert "Cancellation within 14 days" not in base.inner_text
    assert any("Cancellation within 14 days is free." in s.inner_text for s in ex.states)
    assert ex.discovery_closed


async def test_virtualized_list_rows_are_accumulated_before_recycling(
    renderer: BrowserRenderer, server_url: str
):
    """B-9: all 200 rows are kept although only 12 exist in the DOM at any time."""
    base, ex = await _explore(
        renderer,
        f"{server_url}/pages/virtualized_list",
        InteractionPolicy(max_scroll_passes=40),
    )
    nested = next(c for c in ex.controls if c.control_id.startswith("scroll:nested:"))
    assert nested.status == "visited"
    assert nested.reason == "virtualized_rows_accumulated:200:total_unknown"
    rows = ex.states[(nested.state_index or 0) - 1]
    assert rows.label.startswith("virtualized:")
    assert "Rate row 0" in rows.inner_text and "Rate row 199" in rows.inner_text
    assert "Rate row 199" not in base.inner_text
    assert ex.discovery_closed


async def test_virtualized_list_out_of_passes_stays_open(
    renderer: BrowserRenderer, server_url: str
):
    _, ex = await _explore(
        renderer, f"{server_url}/pages/virtualized_list", InteractionPolicy(max_scroll_passes=5)
    )
    nested = next(c for c in ex.controls if c.control_id.startswith("scroll:nested:"))
    assert nested.status == "unvisited"
    assert (nested.reason or "").startswith("scroll_budget_exhausted:virtualized_rows=")
    assert not ex.discovery_closed


async def test_scroll_not_allowed_means_no_scrolling(renderer: BrowserRenderer, server_url: str):
    _, ex = await _explore(
        renderer,
        f"{server_url}/pages/lazy_sections",
        InteractionPolicy(allowed=["expand_disclosure", "select_tab"]),
    )
    assert not any(c.kind == "scroll_surface" for c in ex.controls)
    assert all("fee is waived" not in s.inner_text for s in ex.states)


# -- B-4 to B-12 --------------------------------------------------------------------

EVERYTHING = InteractionPolicy(
    allowed=[
        "scroll",
        "expand_disclosure",
        "select_tab",
        "hover_info",
        "open_informational_dialog",
        "keyboard_traverse",
        "paginate_get",
    ]
)


def _texts(ex: ExplorationResult) -> str:
    return "\n".join(s.inner_text for s in ex.states)


def _one(ex: ExplorationResult, kind: str):
    found = [c for c in ex.controls if c.kind == kind]
    assert len(found) == 1, found
    return found[0]


async def test_carousel_is_advanced_to_its_declared_total(renderer, server_url):
    base, ex = await _explore(renderer, f"{server_url}/pages/carousel", EVERYTHING)
    carousel = _one(ex, "carousel")
    assert (carousel.status, carousel.reason) == ("visited", "known_total:3")
    assert "Slide two" not in base.inner_text
    assert "Slide two: 10% off family plans." in _texts(ex)
    assert "Slide three: no fee for online claims." in _texts(ex)
    assert ex.discovery_closed


async def test_tooltip_and_hover_menu_are_revealed(renderer, server_url):
    _, ex = await _explore(renderer, f"{server_url}/pages/hover_tooltip", EVERYTHING)
    hovers = [c for c in ex.controls if c.kind == "hover"]
    assert {c.label for c in hovers} == {"?", "Products"}
    assert all(c.status == "visited" for c in hovers)
    assert "The deductible resets every January." in _texts(ex)
    assert "Term life insurance" in _texts(ex)


async def test_hover_not_allowed_leaves_tooltip_open_and_excludes_nav(renderer, server_url):
    _, ex = await _explore(
        renderer, f"{server_url}/pages/hover_tooltip", InteractionPolicy(allowed=["scroll"])
    )
    by = {c.label: (c.status, c.reason) for c in ex.controls if c.kind == "hover"}
    assert by["?"] == ("unvisited", "policy_disallows:hover_info")
    assert by["Products"] == ("excluded", "site_navigation_menu")
    assert not ex.discovery_closed


async def test_keyboard_traversal_keeps_focus_only_text(renderer, server_url):
    _, ex = await _explore(renderer, f"{server_url}/pages/focus_reveal", EVERYTHING)
    kb = _one(ex, "keyboard")
    assert kb.status == "visited" and (kb.reason or "").startswith("focus_cycle_complete:")
    assert "Ages 18 to 70 are eligible for this plan." in _texts(ex)


async def test_keyboard_traversal_ends_without_focusable_elements(renderer, server_url):
    _, ex = await _explore(renderer, f"{server_url}/pages/frame_child", EVERYTHING)
    assert _one(ex, "keyboard").reason == "no_focusable_elements"


async def test_dialog_is_opened_captured_and_closed(renderer, server_url):
    _, ex = await _explore(renderer, f"{server_url}/pages/modal_info", EVERYTHING)
    dialog = _one(ex, "dialog")
    assert dialog.status == "visited" and dialog.reason != "dialog_left_open"
    assert "Hospital stays up to 30 days are covered." in _texts(ex)
    assert _one(ex, "disclosure").status == "visited"
    assert ex.discovery_closed


async def test_consent_overlay_is_rejected_and_its_post_blocked(renderer, server_url):
    _, ex = await _explore(renderer, f"{server_url}/pages/consent_banner", EVERYTHING)
    consent = _one(ex, "consent")
    assert consent.status == "visited"
    assert consent.reason == "declared_choice:reject_non_essential;consent_requests_blocked=1"
    assert any(u.startswith("POST ") and "/consent-log" in u for u in ex.unsafe_requests)
    # the overlay no longer intercepts clicks on what is behind it
    assert _one(ex, "disclosure").status == "visited"
    assert "Interest is paid quarterly." in _texts(ex)


async def test_consent_with_only_accept_is_left_in_place(renderer, server_url):
    _, ex = await _explore(renderer, f"{server_url}/pages/consent_accept_only", EVERYTHING)
    consent = _one(ex, "consent")
    assert (consent.status, consent.reason) == (
        "excluded",
        "consent_overlay_left_in_place:only_accept_offered",
    )
    assert ex.discovery_closed


async def test_consent_not_allowed_is_left_in_place(renderer, server_url):
    _, ex = await _explore(
        renderer,
        f"{server_url}/pages/consent_banner",
        InteractionPolicy(allowed=NO_DIALOGS_OR_PAGING),
    )
    consent = _one(ex, "consent")
    assert consent.reason == "consent_overlay_left_in_place:policy"
    assert ex.unsafe_requests == []


async def test_load_more_until_gone_and_next_link_is_a_followup(renderer, server_url):
    _, ex = await _explore(renderer, f"{server_url}/pages/load_more", EVERYTHING)
    more = _one(ex, "load_more")
    assert (more.status, more.reason) == ("visited", "exhausted_after:2")
    assert "Article 5: loaded later." in _texts(ex)
    nxt = _one(ex, "pagination")
    assert (nxt.status, nxt.reason) == ("excluded", "separate_url_followup")
    assert ex.followup_urls == [f"{server_url}/pages/static_ok"]
    assert ex.discovery_closed


async def test_load_more_that_posts_is_blocked(renderer, server_url):
    _, ex = await _explore(renderer, f"{server_url}/pages/load_more_post", EVERYTHING)
    more = _one(ex, "load_more")
    assert more.status == "unvisited"
    assert (more.reason or "").startswith("unsafe_side_effect:POST ")
    assert not ex.discovery_closed


async def test_load_more_not_allowed_is_a_gap(renderer, server_url):
    _, ex = await _explore(
        renderer, f"{server_url}/pages/load_more", InteractionPolicy(allowed=NO_DIALOGS_OR_PAGING)
    )
    assert _one(ex, "load_more").reason == "policy_disallows:paginate_get"


async def test_js_pagination_runs_to_the_last_page(renderer, server_url):
    _, ex = await _explore(renderer, f"{server_url}/pages/js_pagination", EVERYTHING)
    pager = _one(ex, "pagination")
    assert (pager.status, pager.reason) == ("visited", "exhausted_after:2")
    assert "Branch: Chicago office." in _texts(ex)
    assert "Branch: Denver office." in _texts(ex)


async def test_frames_are_read_one_by_one(renderer, server_url):
    base, ex = await _explore(renderer, f"{server_url}/pages/frames", InteractionPolicy())
    frames = [c for c in ex.controls if c.kind == "frame"]
    assert [(c.status, c.reason) for c in frames] == [
        ("visited", "same_origin_read"),
        ("visited", "srcdoc_read"),
        ("visited", "cross_origin_read"),
        ("excluded", "hidden_frame"),
    ]
    by_path = {s.frame_path: s.inner_text for s in ex.states if s.kind == "frame"}
    port = server_url.rsplit(":", 1)[1]
    assert "premiums are paid monthly" in by_path[f"{server_url}/pages/frame_child"]
    assert "rates valid until December" in by_path["srcdoc:2"]
    partner = by_path[f"http://localhost:{port}/pages/frame_partner"]
    assert "claims answered in 48 hours" in partner
    assert "premiums are paid monthly" not in base.inner_text
    assert ex.discovery_closed


async def test_open_and_closed_shadow_roots_are_read(renderer, server_url):
    base, ex = await _explore(renderer, f"{server_url}/pages/shadow_dom", InteractionPolicy())
    shadow = _one(ex, "shadow_root")
    assert (shadow.status, shadow.reason) == ("visited", "composed_tree_read")
    assert ex.composition["open_roots"] == 3
    assert ex.composition["closed_roots"] == ex.composition["closed_roots_read"] == 2
    composed = ex.states[(shadow.state_index or 0) - 1]
    dom = composed.dom_html.decode()
    for text in (
        "Open shadow text: wellness rewards.",
        "Closed shadow text: travel cover.",
        "Declarative closed text: vision included.",
        "Slotted light text: dental included.",
        "Quick links",  # display:contents host
        "Footer link: careers",  # nested root inside it
    ):
        assert text in composed.inner_text
        assert text in dom
    assert "Open shadow text" not in base.inner_text
    assert "Hidden shadow text." not in composed.inner_text
    assert 'data-cp-hidden="1"' in dom.split("Hidden shadow text")[0][-60:]


async def test_virtualized_grid_declares_its_total(renderer, server_url):
    _, ex = await _explore(
        renderer, f"{server_url}/pages/virtualized_grid", InteractionPolicy(max_scroll_passes=40)
    )
    grid = next(c for c in ex.controls if c.control_id.startswith("scroll:nested:"))
    assert (grid.status, grid.reason) == ("visited", "virtualized_rows_accumulated:300/300")
    rows = ex.states[(grid.state_index or 0) - 1]
    assert "Fund row 1\n" in rows.inner_text and "Fund row 300" in rows.inner_text


async def test_live_site_noise_is_not_charged_to_controls(renderer, server_url):
    """Sensor beacons, consent/player chrome and a slow-hiding banner (seen on live sites)."""
    _, ex = await _explore(renderer, f"{server_url}/pages/live_noise", EVERYTHING)
    consent = _one(ex, "consent")
    assert consent.status == "visited", consent.reason
    details = _one(ex, "disclosure")
    assert (details.status, details.reason) == ("visited", None)
    assert "Up to 10% a year without a charge." in _texts(ex)
    chrome = [c for c in ex.controls if c.reason == "third_party_chrome"]
    assert {c.label for c in chrome} >= {"Playback Rate", "Targeting cookies"}
    assert all(c.status == "excluded" for c in chrome)
    assert ex.background_requests_blocked > 0
    assert all("/sensor" in u for u in ex.unsafe_requests)
    assert ex.discovery_closed
