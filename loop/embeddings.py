"""Pluggable text embeddings for Loop's optional vector memory.

EXPERIMENTAL — lives on the `feat/vector-memory-turso` branch, not on `main`.

Answer to "do I need an embedding-model API key?": **no, not necessarily.**

  • ``fastembed`` (default) — runs locally in-process via ONNX Runtime. No API
    key, no GPU, no server. Default model ``BAAI/bge-small-en-v1.5`` (384-dim).
    `pip install "loop[vector]"` pulls it in (downloads the model once, ~90 MB).
  • ``minimax`` — reuses your MiniMax account but a DIFFERENT endpoint
    (``/v1/embeddings``, model ``embo-01``); needs ``MINIMAX_API_KEY`` +
    ``MINIMAX_GROUP_ID``. (The chat model uses the Anthropic-compatible endpoint;
    embeddings do not.)
  • ``openai`` — ``text-embedding-3-small`` (1536-dim); needs ``OPENAI_API_KEY``.
  • ``hashing`` — deterministic, dependency-free bag-of-words hashing. NOT
    semantic; for tests and as a last-resort fallback so nothing crashes.

Select with ``LOOP_EMBEDDINGS=fastembed|minimax|openai|hashing`` (default
``fastembed``). If the chosen provider's dep/key is missing we log and fall back
to ``hashing`` rather than failing the agent.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import urllib.request
from typing import Protocol

log = logging.getLogger("loop.embeddings")

_TOKEN_RE = re.compile(r"[a-z0-9]+")


class Embedder(Protocol):
    dim: int
    name: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...

    def embed_one(self, text: str) -> list[float]: ...


def _l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


class HashingEmbedder:
    """Deterministic, dependency-free embeddings via feature hashing.

    Not semantically rich, but shared tokens land in shared buckets so identical
    or overlapping text scores close — enough for tests and graceful fallback.
    """

    name = "hashing"

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim

    def embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for tok in _TOKEN_RE.findall(text.lower()):
            h = int.from_bytes(hashlib.blake2b(tok.encode(), digest_size=8).digest(), "big")
            vec[h % self.dim] += 1.0
        return _l2_normalize(vec)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_one(t) for t in texts]


class FastEmbedEmbedder:
    """Local ONNX embeddings (no API key). Lazy-imports fastembed."""

    name = "fastembed"

    def __init__(self, model: str | None = None) -> None:
        from fastembed import TextEmbedding  # noqa: PLC0415 (optional dep)

        self.model_name = model or os.environ.get("LOOP_EMBED_MODEL", "BAAI/bge-small-en-v1.5")
        self._model = TextEmbedding(model_name=self.model_name)
        # Derive dimension from a probe (fastembed yields numpy arrays).
        self.dim = len(self.embed_one("dimension probe"))

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [list(map(float, v)) for v in self._model.embed(texts)]

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]


class _HttpEmbedder:
    """Shared JSON-over-HTTP embedding client for hosted providers."""

    name = "http"
    dim = 0

    def _post(self, url: str, headers: dict[str, str], payload: dict) -> dict:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json", **headers}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (configured host)
            return json.loads(resp.read())

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]


class OpenAIEmbedder(_HttpEmbedder):
    name = "openai"

    def __init__(self) -> None:
        self.key = os.environ.get("OPENAI_API_KEY")
        if not self.key:
            raise RuntimeError("OPENAI_API_KEY required for LOOP_EMBEDDINGS=openai")
        self.model = os.environ.get("LOOP_EMBED_MODEL", "text-embedding-3-small")
        self.base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self.dim = self.embed_one("dimension probe").__len__()

    def embed(self, texts: list[str]) -> list[list[float]]:
        data = self._post(
            f"{self.base}/embeddings",
            {"Authorization": f"Bearer {self.key}"},
            {"model": self.model, "input": texts},
        )
        return [row["embedding"] for row in data["data"]]


class MiniMaxEmbedder(_HttpEmbedder):
    """MiniMax embeddings (embo-01). Separate endpoint from the chat model."""

    name = "minimax"

    def __init__(self) -> None:
        self.key = os.environ.get("MINIMAX_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
        self.group = os.environ.get("MINIMAX_GROUP_ID")
        if not self.key or not self.group:
            raise RuntimeError("MINIMAX_API_KEY + MINIMAX_GROUP_ID required for LOOP_EMBEDDINGS=minimax")
        self.model = os.environ.get("LOOP_EMBED_MODEL", "embo-01")
        self.base = os.environ.get("MINIMAX_API_HOST", "https://api.minimax.io").rstrip("/")
        self.dim = self.embed_one("dimension probe").__len__()

    def embed(self, texts: list[str]) -> list[list[float]]:
        data = self._post(
            f"{self.base}/v1/embeddings?GroupId={self.group}",
            {"Authorization": f"Bearer {self.key}"},
            {"model": self.model, "texts": texts, "type": "db"},
        )
        return data.get("vectors") or [row["embedding"] for row in data.get("data", [])]


_PROVIDERS = {
    "fastembed": FastEmbedEmbedder,
    "openai": OpenAIEmbedder,
    "minimax": MiniMaxEmbedder,
    "hashing": HashingEmbedder,
}


def get_embedder() -> Embedder:
    """Build the configured embedder, falling back to hashing on any failure."""
    choice = os.environ.get("LOOP_EMBEDDINGS", "fastembed").strip().lower()
    factory = _PROVIDERS.get(choice, FastEmbedEmbedder)
    try:
        emb = factory()
        log.info("embeddings: using %s (dim=%d)", emb.name, emb.dim)
        return emb
    except Exception as err:  # noqa: BLE001
        log.warning("embeddings: %r unavailable (%s); falling back to hashing", choice, err)
        return HashingEmbedder()
