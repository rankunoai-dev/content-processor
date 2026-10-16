"""I1 URL Content Explorer: embedded data, escaping, CSP, and a real browser pass."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path

import pytest

from src.modules.content_processor.enums import GapType, UrlBadge
from src.modules.content_processor.ui import explorer as ex
from src.modules.content_processor.ui.explorer import explorer_data, render_explorer, write_explorer
from tests.modules.content_processor.l5_factory import block, gap, record

BASE = "https://example.com/"
XSS = '</script><script>window.pwned=1</script><img src=x onerror="window.pwned=2"> &amp;'


def _records():
    return [
        record(
            BASE + "ok",
            blocks=[
                block("ok", 1, "Second", heading=["Plans"]),
                block("ok", 0, "Plans", heading=["Plans"]),
                block("ok", 2, "Menu", role="nav"),
                block("ok", 3, "Again", dedupe_of="ok:b1"),
            ],
        ),
        record(
            BASE + "evil",
            [XSS],
            badge=UrlBadge.BLOCKED,
            gaps=[gap("evil", GapType.ACCESS_BLOCKED, XSS)],
        ),
    ]


def _embedded(page: str) -> dict:
    match = re.search(r'<script type="application/json" id="data">(.*?)</script>', page, re.S)
    assert match is not None
    return json.loads(match.group(1))


def test_data_keeps_reading_order_and_drops_duplicates():
    data = explorer_data(_records(), run_id="r")
    ok = data["records"][0]
    assert [b["text"] for b in ok["blocks"]] == ["Plans", "Second", "Menu"]
    assert [b["boiler"] for b in ok["blocks"]] == [False, False, True]
    assert ok["vector"]["discovery_closed"] is True and ok["words"] == 4


def test_untrusted_text_cannot_break_out_of_the_data_element():
    page = render_explorer(_records(), run_id="run<x>")
    data_start = page.index('id="data">')
    data_end = page.index("</script>", data_start)
    embedded = page[data_start:data_end]
    assert "<" not in embedded.removeprefix('id="data">')
    assert " " not in embedded
    # Exactly two script elements: the data and the pinned page script.
    assert page.count("<script") == 2
    assert _embedded(page)["records"][1]["blocks"][0]["text"] == XSS
    assert "Run run&lt;x&gt;" in page


def test_csp_pins_the_page_script_and_denies_the_network():
    page = render_explorer(_records(), run_id="r", downloads={"R1 <xlsx>": 'r1".xlsx'})
    digest = base64.b64encode(hashlib.sha256(ex._SCRIPT.encode()).digest()).decode()
    assert f"script-src &#x27;sha256-{digest}&#x27;" in page
    assert "default-src &#x27;none&#x27;" in page and "connect-src &#x27;none&#x27;" in page
    assert f"<script>{ex._SCRIPT}</script>" in page
    assert 'href="r1&quot;.xlsx" download>R1 &lt;xlsx&gt;</a>' in page


def test_write_explorer(tmp_path: Path):
    path = tmp_path / "e.html"
    write_explorer(path, _records(), run_id="r")
    assert path.read_text(encoding="utf-8").startswith("<!doctype html>")


@pytest.mark.browser
async def test_explorer_renders_filters_and_never_runs_page_text(tmp_path: Path):
    playwright = pytest.importorskip("playwright.async_api")
    path = tmp_path / "explorer.html"
    write_explorer(path, _records(), run_id="r")
    async with playwright.async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            page = await browser.new_page()
            problems: list[str] = []
            page.on("pageerror", lambda e: problems.append(str(e)))
            page.on("console", lambda m: problems.append(m.text) if m.type == "error" else None)
            await page.goto(path.resolve().as_uri())
            assert await page.locator("#list button").count() == 2
            await page.locator("#list button").nth(1).click()
            assert await page.locator("#detail .block").first.text_content() == XSS
            assert await page.evaluate("window.pwned") is None
            await page.locator("#list button").nth(0).click()
            assert await page.locator("#detail .block").count() == 2  # nav hidden
            await page.check("#boiler")
            assert await page.locator("#detail .block").count() == 3
            await page.locator(".chip", has_text="BLOCKED").click()
            assert await page.locator("#list button").count() == 1
            await page.locator(".chip", has_text="All").click()
            await page.fill("#q", "nothing-matches")
            assert await page.locator("#list").text_content() == "No URL matches the filters."
            await page.set_viewport_size({"width": 375, "height": 800})
            assert await page.evaluate("document.documentElement.scrollWidth") <= 375
            assert problems == []
        finally:
            await browser.close()
