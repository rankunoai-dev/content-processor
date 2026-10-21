"""K20: packed evidence (shared chunks for inlined assets) with exact read-back."""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

import pytest

from src.integrations.evidence_store import (
    CHUNK_MIN_BYTES,
    LocalEvidenceStore,
    pack,
    unpack,
)
from src.modules.content_processor.contracts import Budgets
from src.modules.content_processor.scheduler.budget import BudgetMeter, MeteredEvidenceStore

ROOT = Path(__file__).resolve().parents[3]


def _font(seed: int, size: int = 60_000) -> bytes:
    return base64.b64encode(hashlib.shake_256(b"font%d" % seed).digest(size))


def _page(state: int, font: bytes) -> bytes:
    css = b"@font-face{font-family:x;src:url(data:font/woff2;base64," + font + b")}"
    body = b"".join(b"<p>Paragraph %d of state %d.</p>" % (i, state) for i in range(300))
    logo = b"data:image/png;base64," + _font(99, 8_000)
    return (
        b"<!doctype html><html><head><style>"
        + css
        + b"</style></head><body>"
        + body
        + b'<img src="'
        + logo
        + b'"></body></html>'
    )


def _disk(root: Path) -> int:
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())


def _store(tmp_path: Path) -> LocalEvidenceStore:
    return LocalEvidenceStore(tmp_path, "t1")


# -- pack / unpack -------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"plain text",
        b"<style>" + b"a{b:c}" * 2000 + b"</style>",
        b"<STYLE media=x>" + b"x" * (CHUNK_MIN_BYTES - 1) + b"</STYLE>",
        b"data:" + b"A" * CHUNK_MIN_BYTES + b'"tail',
        b"<style>" + b"data:" + b"B" * 9000 + b") rest</style><p>x</p>",
        bytes(range(256)) * 50,
    ],
    ids=["empty", "plain", "big-style", "style-under-min", "data-uri", "uri-in-style", "binary"],
)
def test_pack_round_trips_exactly(data: bytes):
    container, chunks = pack(data)
    assert unpack(container, chunks.__getitem__) == data


def test_large_inlined_assets_become_shared_chunks():
    font = _font(1)
    _, chunks = pack(_page(1, font))
    assert any(font in c for c in chunks.values())
    assert all(len(c) >= CHUNK_MIN_BYTES for c in chunks.values())
    small = b"<style>a{b:c}</style>data:" + b"x" * 10
    assert pack(small)[1] == {}


def test_unpack_rejects_foreign_and_damaged_input():
    with pytest.raises(ValueError, match="not a packed"):
        unpack(b"raw bytes", lambda _s: b"")
    container, chunks = pack(_page(1, _font(1)))
    with pytest.raises(ValueError, match="missing or damaged"):
        unpack(container, lambda _s: b"short")


# -- store ---------------------------------------------------------------------------


def test_explored_states_share_their_fonts(tmp_path: Path):
    store = _store(tmp_path)
    font = _font(7)
    pages = [_page(i, font) for i in range(6)]
    refs = [store.put(p, "text/html") for p in pages]
    raw = sum(len(p) for p in pages)
    assert _disk(tmp_path) < raw / 8
    assert sum(r.stored_bytes or 0 for r in refs) == _disk(tmp_path)
    assert refs[1].stored_bytes is not None and refs[1].stored_bytes < refs[0].stored_bytes / 5
    for page, ref in zip(pages, refs, strict=True):
        assert ref.sha256 == hashlib.sha256(page).hexdigest()
        assert store.exists(ref.sha256) and store.get(ref.sha256) == page
        assert ref.uri.endswith(".cpz")


def test_reused_objects_cost_nothing(tmp_path: Path):
    store = _store(tmp_path)
    page = _page(1, _font(1))
    first, again = store.put(page, "text/html"), store.put(page, "text/html")
    assert not first.reused and again.reused and again.stored_bytes == 0


@pytest.mark.parametrize(
    ("data", "media"),
    [
        (b"\xff\xd8\xff" + os.urandom(20_000), "image/jpeg"),
        (b"%PDF-1.7" + os.urandom(20_000), "application/pdf"),
        (b"short", "text/plain"),
        (os.urandom(30_000), "text/plain"),  # incompressible: not worth packing
    ],
    ids=["jpeg", "pdf", "tiny", "incompressible"],
)
def test_media_and_incompressible_payloads_stay_raw(tmp_path: Path, data: bytes, media: str):
    store = _store(tmp_path)
    ref = store.put(data, media)
    assert ref.stored_bytes == len(data)
    assert not ref.uri.endswith(".cpz") and store.get(ref.sha256) == data


def test_objects_written_raw_before_k20_are_still_read(tmp_path: Path):
    data = _page(3, _font(3))
    sha = hashlib.sha256(data).hexdigest()
    legacy = tmp_path / "t1" / "raw" / sha[:2] / sha[2:4] / sha
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(data)
    store = _store(tmp_path)
    assert store.exists(sha) and store.get(sha) == data
    assert store.put(data, "text/html").reused


def test_repack_converts_raw_objects_and_keeps_damaged_ones(tmp_path: Path):
    store = _store(tmp_path)
    good = _page(4, _font(4))
    sha = hashlib.sha256(good).hexdigest()
    raw = tmp_path / "t1" / "raw" / sha[:2] / sha[2:4] / sha
    raw.parent.mkdir(parents=True)
    raw.write_bytes(good)
    saved = store.repack(sha)
    assert 0 < saved < len(good)
    assert not raw.exists() and store.get(sha) == good
    assert store.repack(sha) == 0, "already packed"
    damaged_sha = "ab" * 32
    damaged = tmp_path / "t1" / "raw" / "ab" / "ab" / damaged_sha
    damaged.parent.mkdir(parents=True, exist_ok=True)
    damaged.write_bytes(good)  # content does not match its name
    assert store.repack(damaged_sha) == 0 and damaged.exists()
    assert store.repack("cd" * 32) == 0, "absent object"


def test_budget_meters_bytes_on_disk(tmp_path: Path):
    meter = BudgetMeter(Budgets(max_evidence_bytes=10_000_000))
    store = MeteredEvidenceStore(_store(tmp_path), meter)
    pages = [_page(i, _font(5)) for i in range(4)]
    for page in pages:
        store.put(page, "text/html")
    assert meter.evidence_bytes == _disk(tmp_path)
    assert meter.evidence_bytes < sum(len(p) for p in pages) / 5


def test_repack_script(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    import importlib.util

    spec = importlib.util.spec_from_file_location("repack", ROOT / "scripts" / "repack_evidence.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    page = _page(9, _font(9))
    sha = hashlib.sha256(page).hexdigest()
    raw = tmp_path / "t1" / "raw" / sha[:2] / sha[2:4] / sha
    raw.parent.mkdir(parents=True)
    raw.write_bytes(page)
    jpeg = b"\xff\xd8\xff" + os.urandom(5000)
    jsha = hashlib.sha256(jpeg).hexdigest()
    jpath = tmp_path / "t1" / "raw" / jsha[:2] / jsha[2:4] / jsha
    jpath.parent.mkdir(parents=True, exist_ok=True)
    jpath.write_bytes(jpeg)
    assert module.main(["--root", str(tmp_path), "--dry-run"]) == 0
    assert raw.exists()
    assert module.main(["--root", str(tmp_path), "--tenant", "t1"]) == 0
    out = capsys.readouterr().out
    assert "t1: 2 objects, 1 packed" in out
    assert not raw.exists() and jpath.exists()
    assert LocalEvidenceStore(tmp_path, "t1").get(sha) == page
