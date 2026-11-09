"""Embedders for the knowledge base (RFC 0002, Stage D) and a check of the model server.

- `LocalEmbedder` (in `assist.py`) calls Ollama's OpenAI-compatible `/embeddings`
  on this machine or the private network: `bge-m3`, 1,024 numbers per text.
- `CpuEmbedder` (in `cpu_embedder.py`) is the first fallback: a small multilingual
  sentence model run on the CPU (fastembed), meaning at $0 without a server.
- `HashingEmbedder` is the last fallback: word and word-pair counts hashed into
  1,024 buckets. It needs nothing installed and is fully repeatable, but it only
  matches shared words, not meaning.

`auto` takes the best one available: bge-m3 on Ollama, else the CPU model, else
hashing. The store records which model made its vectors (`model_rank` orders them),
and indexing with a better model rebuilds a store made with a worse one.
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
from src.modules.content_processor.knowledge.cpu_embedder import (
    CPU_PREFIX,
    CpuEmbedder,
    cpu_available,
    shared_cpu_embedder,
)

__all__ = [
    "HASHING_DIMS",
    "HASHING_MODEL",
    "Embedder",
    "EmbedderChoice",
    "HashingEmbedder",
    "ModelServer",
    "check_model_server",
    "embedder_for_model",
    "is_semantic",
    "model_kind",
    "model_rank",
    "open_embedder",
]

HASHING_MODEL = "hashing-1024-v1"
HASHING_DIMS = 1024
_WORD = re.compile(r"[^\W_]+", re.UNICODE)

EmbedderChoice = Literal["auto", "ollama", "cpu", "hashing"]


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


def model_kind(model: str) -> Literal["model server", "CPU fallback", "words only"]:
    """In plain words, what made a store's vectors."""
    if model.startswith("hashing-"):
        return "words only"
    if model.startswith(CPU_PREFIX):
        return "CPU fallback"
    return "model server"


def model_rank(model: str | None) -> int:
    """How good a store's vectors are: 0 hashing, 1 CPU fallback, 2 model server."""
    if not model:
        return -1
    return {"words only": 0, "CPU fallback": 1, "model server": 2}[model_kind(model)]


def _cpu(model_name: str | None = None) -> CpuEmbedder:
    from src.core.config import get_settings

    settings = get_settings()
    name = model_name or settings.cp_fallback_embed_model
    if not name:
        msg = "the CPU fallback is switched off (CP_FALLBACK_EMBED_MODEL is empty)"
        raise RuntimeError(msg)
    return shared_cpu_embedder(name, dims=HASHING_DIMS, cache_dir=settings.cp_model_cache_dir)


def embedder_for_model(model: str, endpoint: str) -> Embedder:
    """The embedder that made a store's vectors (to embed queries the same way).

    Raises `RuntimeError` or `LocalOnlyError` when it is not available now.
    """
    if model.startswith("hashing-"):
        return HashingEmbedder()
    if model.startswith(CPU_PREFIX):
        return _cpu(model.removeprefix(CPU_PREFIX))
    embedder, _note = open_embedder("ollama", endpoint, model)
    return embedder


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

    `auto` uses Ollama when it is reachable and has the model, else the CPU fallback
    model, else the hashing stand-in. `ollama` insists (raises `LocalOnlyError` for a
    refused endpoint, `RuntimeError` when the model is missing). `cpu` insists on the
    CPU model. `hashing` never calls a model.
    """
    if choice == "hashing" or model.startswith("hashing-"):
        return HashingEmbedder(), "hashing fallback (asked for)"
    if choice == "cpu" or model.startswith(CPU_PREFIX):
        cpu = _cpu(model.removeprefix(CPU_PREFIX) if model.startswith(CPU_PREFIX) else None)
        return cpu, f"CPU fallback {cpu.model_name} (asked for)"
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
    if cpu_available():
        try:
            cpu = _cpu()
        except RuntimeError as exc:
            return HashingEmbedder(), f"hashing fallback: {reason}; {exc}"
        return cpu, f"CPU fallback {cpu.model_name}: {reason}"
    return HashingEmbedder(), f"hashing fallback: {reason} (no CPU model: fastembed not installed)"
