"""Model endpoints: local, in-house by name, or an opted-in https cloud server; never plain http."""

from __future__ import annotations

import socket
from collections.abc import Iterator
from typing import Any

import pytest

from src.core.config import get_settings
from src.modules.content_processor.analysis import nli
from src.modules.content_processor.analysis.nli import (
    GatewayJudge,
    JudgeRefusedError,
    endpoint_decision,
    model_endpoint_allowed,
    model_headers,
)
from src.modules.content_processor.knowledge.assist import LocalOnlyError
from src.modules.content_processor.knowledge.embedders import check_model_server, open_embedder

NAMES = {
    "ollama.office.lan": ["10.20.0.5"],
    "gpu.vpn.internal": ["172.16.4.2", "fd00::7"],
    "ollama.example.com": ["8.8.8.8"],
    "split.example.com": ["10.0.0.1", "1.1.1.1"],
}


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def resolve(host: str, *_a: Any, **_k: Any) -> list[tuple[Any, ...]]:
        if host not in NAMES:
            raise socket.gaierror("unknown host")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 0)) for a in NAMES[host]]

    monkeypatch.setattr(nli.socket, "getaddrinfo", resolve)
    nli._resolves_private.cache_clear()
    yield
    nli._resolves_private.cache_clear()


@pytest.mark.parametrize(
    ("url", "allowed", "why"),
    [
        ("http://localhost:11434/v1", True, "local or private address"),
        ("http://192.168.1.50:11434/v1", True, "local or private address"),
        ("http://10.0.0.8:11434/v1", True, "local or private address"),
        ("http://ollama.office.lan:11434/v1", True, "in-house server"),
        ("http://gpu.vpn.internal:11434/v1", True, "in-house server"),
        ("https://ollama.example.com/v1", False, "set CP_ALLOW_REMOTE_MODEL_ENDPOINT=true"),
        ("http://split.example.com/v1", False, "only local or private-network"),
        ("http://nowhere.invalid/v1", False, "only local or private-network"),
    ],
)
def test_without_the_flag(url: str, allowed: bool, why: str) -> None:
    ok, reason = endpoint_decision(url, allow_remote=False)
    assert ok is allowed and why in reason


def test_a_cloud_server_needs_the_flag_and_https() -> None:
    assert endpoint_decision("https://ollama.example.com/v1", allow_remote=True) == (
        True,
        "remote server allowed by CP_ALLOW_REMOTE_MODEL_ENDPOINT",
    )
    assert endpoint_decision("http://ollama.example.com:11434/v1", allow_remote=True) == (
        False,
        "a remote model endpoint must use https",
    )
    assert model_endpoint_allowed("http://localhost:11434/v1", allow_remote=False)


def test_settings_drive_the_rule_and_the_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CP_ALLOW_REMOTE_MODEL_ENDPOINT", "true")
    monkeypatch.setenv("CP_MODEL_API_KEY", "secret-token")
    get_settings.cache_clear()
    try:
        assert model_endpoint_allowed("https://ollama.example.com/v1")
        assert model_headers() == {"Authorization": "Bearer secret-token"}
        judge = GatewayJudge("https://ollama.example.com/v1", "qwen2.5:7b-instruct")
        assert judge.model == "qwen2.5:7b-instruct"
    finally:
        monkeypatch.delenv("CP_ALLOW_REMOTE_MODEL_ENDPOINT")
        monkeypatch.delenv("CP_MODEL_API_KEY")
        get_settings.cache_clear()
    assert model_headers() == {}
    with pytest.raises(JudgeRefusedError, match="CP_ALLOW_REMOTE_MODEL_ENDPOINT"):
        GatewayJudge("https://ollama.example.com/v1", "m")
    with pytest.raises(LocalOnlyError, match="only local or private-network"):
        open_embedder("ollama", "https://ollama.example.com/v1", "bge-m3")
    refused = check_model_server("https://ollama.example.com/v1")
    assert not refused.reachable and refused.detail.startswith("refused: only local")
