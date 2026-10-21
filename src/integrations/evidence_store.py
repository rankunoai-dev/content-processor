"""Content-addressed evidence store (ARCHITECTURE.md section 10).

Stage 1 uses the local filesystem; MinIO/S3 slots in behind the same
interface. Objects are immutable: a hash that exists is never rewritten, so
the same PDF linked from 200 pages costs one write and one fetch.

Storage format (K20). An object is addressed by the SHA-256 of its exact
bytes, and `get()` always returns those exact bytes; only the bytes on disk
change shape:

* Already-compressed media (images, PDF, archives, fonts, audio, video) are
  stored as they are, at `raw/<aa>/<bb>/<sha256>`.
* Text payloads (DOM snapshots, JSON, text) are packed: every inlined
  `data:` URI and every `<style>` body of `CHUNK_MIN_BYTES` or more becomes a
  chunk, stored once per tenant under `chunks/<aa>/<sha256>.z` (zlib), and the
  rest is compressed into a container at `raw/<aa>/<bb>/<sha256>.cpz`. A page's
  explored states, and pages of one site, share their inlined fonts and
  stylesheets, so those bytes are written once instead of per snapshot:
  measured on a live run, 3 MB DOM snapshots shrink about 20-35 times.
* When packing would not save at least 10 %, the object is stored raw.

Chunks are written before their container, both atomically (temp file and
rename), so a reader never sees a container whose chunks are missing.
Objects stored raw by earlier versions are read as before.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from pydantic import Field

from src.core.schemas import StrictModel

__all__ = [
    "CHUNK_MIN_BYTES",
    "EvidenceRef",
    "EvidenceStore",
    "LocalEvidenceStore",
    "pack",
    "unpack",
]

CHUNK_MIN_BYTES = 4096
"""Inlined assets at least this large are shared chunks; smaller ones stay inline."""

_MAGIC = b"CPZ1"
_MIN_SAVING = 0.10
_PRECOMPRESSED_PREFIXES = ("image/", "audio/", "video/", "font/")
_PRECOMPRESSED_TYPES = frozenset(
    {
        "application/pdf",
        "application/zip",
        "application/gzip",
        "application/x-gzip",
        "application/x-7z-compressed",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }
)
_DATA_URI = re.compile(rb"data:[^\"')\s<>]{%d,}" % CHUNK_MIN_BYTES)
_STYLE_BODY = re.compile(rb"(<style\b[^>]*>)(.*?)(</style\s*>)", re.IGNORECASE | re.DOTALL)


class EvidenceRef(StrictModel):
    """Pointer to one immutable object."""

    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    uri: str
    bytes: int = Field(ge=0)
    media_type: str
    reused: bool = False
    stored_bytes: int | None = Field(
        default=None,
        ge=0,
        description="Bytes this put added on disk (container plus new chunks); 0 when reused.",
    )


class EvidenceStore(Protocol):
    """Minimal store contract used by workers."""

    def put(self, data: bytes, media_type: str) -> EvidenceRef:
        """Store bytes; returns a ref (reused=True if the hash already existed)."""
        ...

    def get(self, sha256: str) -> bytes:
        """Read bytes back by hash."""
        ...

    def exists(self, sha256: str) -> bool:
        """True when the hash is already stored."""
        ...


# -- packing ---------------------------------------------------------------------

Part = tuple[bytes, bool]
"""(bytes, is_chunk)."""


def _split(parts: list[Part], pattern: re.Pattern[bytes], group: int) -> list[Part]:
    out: list[Part] = []
    for data, is_chunk in parts:
        if is_chunk:
            out.append((data, True))
            continue
        pos = 0
        for match in pattern.finditer(data):
            start, end = match.span(group)
            if end - start < CHUNK_MIN_BYTES:
                continue
            if start > pos:
                out.append((data[pos:start], False))
            out.append((data[start:end], True))
            pos = end
        if pos < len(data):
            out.append((data[pos:], False))
    return out


def pack(data: bytes) -> tuple[bytes, dict[str, bytes]]:
    """Split `data` into a container and its chunks (by SHA-256 of each chunk's bytes).

    `unpack(container, chunks.__getitem__)` returns `data` exactly.
    """
    parts = _split([(data, False)], _DATA_URI, 0)
    parts = _split(parts, _STYLE_BODY, 2)
    layout: list[list[int | str]] = []
    literals: list[bytes] = []
    chunks: dict[str, bytes] = {}
    for piece, is_chunk in parts:
        if is_chunk:
            sha = hashlib.sha256(piece).hexdigest()
            chunks[sha] = piece
            layout.append([1, sha, len(piece)])
        else:
            literals.append(piece)
            layout.append([0, len(piece)])
    header = json.dumps({"v": 1, "parts": layout}, separators=(",", ":")).encode()
    body = zlib.compress(b"".join(literals), 6)
    return _MAGIC + len(header).to_bytes(4, "big") + header + body, chunks


def unpack(container: bytes, chunk: Callable[[str], bytes]) -> bytes:
    """Rebuild the original bytes; `chunk(sha256)` supplies each shared chunk."""
    if not container.startswith(_MAGIC):
        msg = "not a packed evidence container"
        raise ValueError(msg)
    size = int.from_bytes(container[4:8], "big")
    header = json.loads(container[8 : 8 + size])
    literals = zlib.decompress(container[8 + size :])
    out: list[bytes] = []
    pos = 0
    for part in header["parts"]:
        if part[0] == 0:
            out.append(literals[pos : pos + part[1]])
            pos += part[1]
        else:
            piece = chunk(part[1])
            if len(piece) != part[2]:
                msg = f"chunk {part[1]} is missing or damaged"
                raise ValueError(msg)
            out.append(piece)
    return b"".join(out)


def _precompressed(media_type: str) -> bool:
    main = media_type.split(";", 1)[0].strip().lower()
    return main in _PRECOMPRESSED_TYPES or main.startswith(_PRECOMPRESSED_PREFIXES)


def _write_atomic(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class LocalEvidenceStore:
    """Filesystem store under `<root>/<tenant>/` (`raw/` objects, `chunks/` shared parts)."""

    def __init__(self, root: Path, tenant_id: str) -> None:
        """Create the tenant prefix on first use."""
        self._base = Path(root) / tenant_id / "raw"
        self._chunks = Path(root) / tenant_id / "chunks"
        self._base.mkdir(parents=True, exist_ok=True)

    def _path(self, sha256: str) -> Path:
        return self._base / sha256[:2] / sha256[2:4] / sha256

    def _packed_path(self, sha256: str) -> Path:
        return self._base / sha256[:2] / sha256[2:4] / f"{sha256}.cpz"

    def _chunk_path(self, sha256: str) -> Path:
        return self._chunks / sha256[:2] / f"{sha256}.z"

    def _read_chunk(self, sha256: str) -> bytes:
        return zlib.decompress(self._chunk_path(sha256).read_bytes())

    def put(self, data: bytes, media_type: str) -> EvidenceRef:
        """Write once: packed when that saves space, raw otherwise."""
        sha256 = hashlib.sha256(data).hexdigest()
        if self.exists(sha256):
            return EvidenceRef(
                sha256=sha256,
                uri=self._uri(sha256),
                bytes=len(data),
                media_type=media_type,
                reused=True,
                stored_bytes=0,
            )
        stored = self._put_packed(sha256, data) if not _precompressed(media_type) else None
        if stored is None:
            _write_atomic(self._path(sha256), data)
            stored = len(data)
        return EvidenceRef(
            sha256=sha256,
            uri=self._uri(sha256),
            bytes=len(data),
            media_type=media_type,
            stored_bytes=stored,
        )

    def _put_packed(self, sha256: str, data: bytes) -> int | None:
        container, chunks = pack(data)
        new_chunks = {
            sha: zlib.compress(piece, 6)
            for sha, piece in chunks.items()
            if not self._chunk_path(sha).exists()
        }
        cost = len(container) + sum(len(z) for z in new_chunks.values())
        if cost > len(data) * (1 - _MIN_SAVING):
            return None
        for sha, compressed in new_chunks.items():
            _write_atomic(self._chunk_path(sha), compressed)
        _write_atomic(self._packed_path(sha256), container)
        return cost

    def _uri(self, sha256: str) -> str:
        packed = self._packed_path(sha256)
        return (packed if packed.exists() else self._path(sha256)).as_uri()

    def get(self, sha256: str) -> bytes:
        """Read the exact original bytes back by hash."""
        packed = self._packed_path(sha256)
        if packed.exists():
            return unpack(packed.read_bytes(), self._read_chunk)
        return self._path(sha256).read_bytes()

    def exists(self, sha256: str) -> bool:
        """True when the hash is already stored (raw or packed)."""
        return self._path(sha256).exists() or self._packed_path(sha256).exists()

    def repack(self, sha256: str) -> int:
        """Convert one raw object to the packed form when that saves space; bytes saved."""
        raw = self._path(sha256)
        if not raw.exists() or self._packed_path(sha256).exists():
            return 0
        data = raw.read_bytes()
        if hashlib.sha256(data).hexdigest() != sha256:
            return 0  # leave a damaged object for the integrity rule to report
        stored = self._put_packed(sha256, data)
        if stored is None:
            return 0
        if unpack(self._packed_path(sha256).read_bytes(), self._read_chunk) != data:
            self._packed_path(sha256).unlink()
            return 0
        raw.unlink()
        return len(data) - stored
