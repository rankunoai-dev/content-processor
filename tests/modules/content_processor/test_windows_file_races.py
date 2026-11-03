"""Windows file quirks: a relative evidence root, and a file swap refused for a moment."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.integrations.evidence_store import LocalEvidenceStore
from src.modules.content_processor.projects import store as store_module


def test_a_relative_evidence_root_still_gives_file_uris(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    store = LocalEvidenceStore(Path("./evidence"), "t1")
    ref = store.put(b"<html>hello</html>", "text/html")
    assert ref.uri.startswith("file:///")
    monkeypatch.chdir(tmp_path.parent)  # a later change of folder does not move the store
    assert store.get(ref.sha256) == b"<html>hello</html>"


def test_a_refused_swap_is_retried_then_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = os.replace
    refusals = {"left": 3}

    def flaky(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        if refusals["left"] > 0:
            refusals["left"] -= 1
            raise PermissionError(5, "Access is denied")
        real(src, dst)

    monkeypatch.setattr(store_module.os, "replace", flaky)
    monkeypatch.setattr(store_module.time, "sleep", lambda _s: None)
    target = tmp_path / "state.json"
    store_module._write_json(target, {"status": "running"})
    assert target.read_text(encoding="utf-8").count("running") == 1

    def always(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(store_module.os, "replace", always)
    with pytest.raises(PermissionError):
        store_module._write_json(target, {"status": "finished"})
    assert "running" in target.read_text(encoding="utf-8")
    assert not list(tmp_path.glob("*.tmp"))  # the temporary file is cleaned up
