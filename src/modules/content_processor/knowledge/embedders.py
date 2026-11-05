"""Embedders for the knowledge base (RFC 0002, Stage D) and a check of the model server.

- `LocalEmbedder` (in `assist.py`) calls Ollama's OpenAI-compatible `/embeddings`
  on this machine or the private network: `bge-m3`, 1,024 numbers per text.
- `HashingEmbedder` is the offline fallback: word and word-pair counts hashed into
  1,024 buckets. It needs nothing installed and is fully repeatable, but it only
  matches shared words, not meaning. The store records which model made its
  vectors, so a store built with it is rebuilt when `bge-m3` becomes available.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Literal, Protocol

import httpx

from src.modules.content_processor.analysis.nli import endpoint_decision, model_headers
from src.modules.content_processor.knowledge.assist import LocalEmbedder, LocalOnlyError

__all__ = [
    "HASHING_DIMS",
    "HASHING_MODEL",
    "Embedder",
    "EmbedderChoice",
    "HashingEmbedder",
    "ModelServer",
    "check_model_server",
    "is_semantic",
    "open_embedder",
]

HASHING_MODEL = "hashing-1024-v1"
HASHING_DIMS = 1024
_WORD = re.compile(r"[^\W_]+", re.UNICODE)

EmbedderChoice = Literal["auto", "ollama", "hashing"]


class Embedder(Protocol):
    """Anything that turns texts into vectors of one fixed length."""

    model: str

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        """One vector per text, or None when the call fails."""
        ...

    def close(self) -> None:
        """Release the connection, if any."""
        ...


def is_semantic(model: str) -> bool:
    """True for a real embedding model (meaning), False for the hashing fallback (words)."""
    return not model.startswith("hashing-")


class HashingEmbedder:
    """Deterministic word-hashing vectors: offline, $0, matches shared words only."""

    def __init__(self, dims: int = HASHING_DIMS) -> None:
        """Vectors of `dims` numbers, unit length."""
        self.dims = dims
        self.model = HASHING_MODEL if dims == HASHING_DIMS else f"hashing-{dims}-v1"

    def _vector(self, text: str) -> list[float]:
        words = [w.casefold() for w in _WORD.findall(text)]
        features = Counter(words)
        features.update(f"{a} {b}" for a, b in zip(words, words[1:], strict=False))
        vector = [0.0] * self.dims
        for feature, count in features.items():
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "little") % self.dims
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[bucket] += sign * (1.0 + math.log(count))
        norm = math.sqrt(sum(x * x for x in vector))
        return [x / norm for x in vector] if norm else vector

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        """One vector per text; never fails."""
        return [self._vector(t) for t in texts]

    def close(self) -> None:
        """Nothing to release."""


@dataclass
class ModelServer:
    """What the model server at an endpoint offers."""

    endpoint: str
    reachable: bool
    models: list[str] = field(default_factory=list)
    detail: str = ""

    def has(self, model: str) -> bool:
        """True when `model` (or `model:latest`) is pulled on the server."""
        wanted = {model, f"{model}:latest"} if ":" not in model else {model}
        return any(m in wanted for m in self.models)


def check_model_server(endpoint: str, *, client: httpx.Client | None = None) -> ModelServer:
    """List the models of an Ollama server (`<endpoint>/models`); never raises."""
    allowed, why = endpoint_decision(endpoint)
    if not allowed:
        return ModelServer(endpoint, False, detail=f"refused: {why}")
    http = client or httpx.Client(timeout=5.0, trust_env=False, headers=model_headers())
    try:
        response = http.get(endpoint.rstrip("/") + "/models")
        response.raise_for_status()
        data = response.json().get("data", [])
        models = sorted(str(item["id"]) for item in data if isinstance(item, dict))
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return ModelServer(endpoint, False, detail=f"{type(exc).__name__}: {exc}"[:300])
    finally:
        if client is None:
            http.close()
    return ModelServer(endpoint, True, models=models)


def open_embedder(
    choice: EmbedderChoice,
    endpoint: str,
    model: str,
    *,
    server: ModelServer | None = None,
) -> tuple[Embedder, str]:
    """The embedder to use and a plain note on why.

    `auto` uses Ollama when it is reachable and has the model, else the hashing
    fallback. `ollama` insists (raises `LocalOnlyError` for a non-local endpoint,
    `RuntimeError` when the model is missing). `hashing` never calls a model.
    """
    if choice == "hashing" or model.startswith("hashing-"):
        return HashingEmbedder(), "hashing fallback (asked for)"
    allowed, why = endpoint_decision(endpoint)
    if not allowed:
        msg = f"refused model endpoint {endpoint}: {why}"
        raise LocalOnlyError(msg)
    status = server or check_model_server(endpoint)
    if status.reachable and status.has(model):
        return LocalEmbedder(endpoint, model), f"ollama {model} at {endpoint}"
    reason = (
        f"model {model} is not pulled (run: ollama pull {model})"
        if status.reachable
        else f"no model server at {endpoint} ({status.detail})"
    )
    if choice == "ollama":
        raise RuntimeError(reason)
    return HashingEmbedder(), f"hashing fallback: {reason}"
