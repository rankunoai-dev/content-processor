"""Which browser renders pages: the installed Chrome, or Playwright's bundled Chromium.

A container has no Google Chrome: `CP_BROWSER_CHANNEL=chromium` asks for the bundled
Chromium straight away, and even the default "chrome" falls back to it when the
Chrome launch fails (no second configuration to get wrong).
"""

from __future__ import annotations

from typing import Any

import pytest

from src.core.config import reset_settings_cache
from src.integrations import browser_client
from src.integrations.browser_client import BrowserRenderer, PlaywrightError
from src.modules.content_processor import cli
from src.modules.content_processor.contracts import RunScope


class _FakeBrowser:
    version = "bundled 131"

    async def close(self) -> None:
        return None


class _FakeChromium:
    def __init__(self, launches: list[str | None]) -> None:
        self.launches = launches

    async def launch(self, *, channel: str | None = None, headless: bool = True) -> _FakeBrowser:
        self.launches.append(channel)
        if channel is not None:
            msg = f"Chromium distribution '{channel}' is not found"
            raise PlaywrightError(msg)
        return _FakeBrowser()


class _FakePlaywright:
    def __init__(self, launches: list[str | None]) -> None:
        self.chromium = _FakeChromium(launches)

    async def stop(self) -> None:
        return None


class _FakeStarter:
    def __init__(self, launches: list[str | None]) -> None:
        self._launches = launches

    async def start(self) -> _FakePlaywright:
        return _FakePlaywright(self._launches)


def test_channel_comes_from_the_setting(scope: RunScope, monkeypatch: pytest.MonkeyPatch) -> None:
    assert cli.browser_channel() == "chrome", "a workstation uses the installed Chrome"
    assert cli.build_renderer(scope)._channel == "chrome"
    monkeypatch.setenv("CP_BROWSER_CHANNEL", "chromium")
    reset_settings_cache()
    assert cli.browser_channel() is None, "None asks Playwright for its bundled Chromium"
    assert cli.build_renderer(scope)._channel is None
    monkeypatch.setenv("CP_BROWSER_CHANNEL", "firefox")
    reset_settings_cache()
    with pytest.raises(ValueError, match="cp_browser_channel"):
        cli.browser_channel()


async def test_chrome_channel_falls_back_to_bundled_chromium(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launches: list[str | None] = []
    monkeypatch.setattr(browser_client, "async_playwright", lambda: _FakeStarter(launches))

    def renderer(channel: str | None) -> Any:
        return BrowserRenderer(
            user_agent="t/1", host_checker=lambda _h: (True, ""), channel=channel
        )

    chrome = renderer("chrome")
    await chrome.start()
    assert launches == ["chrome", None], "Chrome tried first, then the bundled Chromium"
    assert chrome.browser_version == "bundled 131"
    await chrome.stop()

    launches.clear()
    bundled = renderer(None)
    await bundled.start()
    assert launches == [None], "chromium: the bundled browser straight away"
    await bundled.stop()
