"""Content-addressed evidence store (ARCHITECTURE.md section 10).

Stage 1 uses the local filesystem; MinIO/S3 slots in behind the same
interface. Objects are immutable: a hash that exists is never rewritten, so
the same PDF linked from 200 pages costs one write and one fetch.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Protocol

from pydantic import Field

from src.core.schemas import StrictModel

__all__ = ["EvidenceRef", "EvidenceStore", "LocalEvidenceStore"]


class EvidenceRef(StrictModel):
    """Pointer to one immutable object."""

    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    uri: str
    bytes: int = Field(ge=0)
    media_type: str
    reused: bool = False


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


class LocalEvidenceStore:
    """Filesystem store: `<root>/<tenant>/raw/<aa>/<bb>/<sha256>`."""

    def __init__(self, root: Path, tenant_id: str) -> None:
        """Create the tenant prefix on first use."""
        self._base = Path(root) / tenant_id / "raw"
        self._base.mkdir(parents=True, exist_ok=True)

    def _path(self, sha256: str) -> Path:
        return self._base / sha256[:2] / sha256[2:4] / sha256

    def put(self, data: bytes, media_type: str) -> EvidenceRef:
        """Write once, atomically (temp file + rename)."""
        sha256 = hashlib.sha256(data).hexdigest()
        target = self._path(sha256)
        if target.exists():
            return EvidenceRef(
                sha256=sha256,
                uri=target.as_uri(),
                bytes=len(data),
                media_type=media_type,
                reused=True,
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(tmp, target)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return EvidenceRef(
            sha256=sha256, uri=target.as_uri(), bytes=len(data), media_type=media_type
        )

    def get(self, sha256: str) -> bytes:
        """Read bytes back by hash."""
        return self._path(sha256).read_bytes()

    def exists(self, sha256: str) -> bool:
        """True when the hash is already stored."""
        return self._path(sha256).exists()
