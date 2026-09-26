"""Embeddings : fastembed local (défaut, faible latence à la requête), Gemini (API), hashing (offline/tests)."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import unicodedata
from typing import Protocol

import numpy as np

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)
_TOKEN = re.compile(r"\w+", re.UNICODE)


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


class Embedder(Protocol):
    name: str
    dim: int
    confident_cosine: float

    async def embed_documents(self, texts: list[str]) -> np.ndarray: ...
    async def embed_query(self, text: str) -> np.ndarray: ...


def _l2(m: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(m, axis=-1, keepdims=True)
    n[n == 0] = 1.0
    return (m / n).astype(np.float32)


class HashingEmbedder:
    """Feature hashing (mots + trigrammes de caractères). Déterministe, zéro dépendance, ~0.1 ms/texte."""

    name = "hashing"
    confident_cosine = 0.28

    def __init__(self, dim: int = 768) -> None:
        self.dim = dim

    def _vec(self, text: str) -> np.ndarray:
        v = np.zeros(self.dim, dtype=np.float32)
        words = _TOKEN.findall(normalize(text))
        feats = [w for w in words if len(w) > 1]
        for w in words:
            padded = f"#{w}#"
            feats.extend(padded[i : i + 3] for i in range(len(padded) - 2))
        for f in feats:
            h = int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "little")
            v[h % self.dim] += 1.0 if (h >> 63) & 1 else -1.0
        return v

    async def embed_documents(self, texts: list[str]) -> np.ndarray:
        return _l2(np.stack([self._vec(t) for t in texts])) if texts else np.zeros((0, self.dim), np.float32)

    async def embed_query(self, text: str) -> np.ndarray:
        return _l2(self._vec(text)[None, :])[0]


class FastEmbedEmbedder:
    name = "fastembed"
    confident_cosine = 0.45

    def __init__(self, model_name: str, cache_dir: str) -> None:
        from fastembed import TextEmbedding

        self._model = TextEmbedding(model_name=model_name, cache_dir=cache_dir)
        self.dim = len(next(iter(self._model.embed(["warmup"]))))
        self.name = f"fastembed:{model_name.split('/')[-1]}"

    async def embed_documents(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), np.float32)
        return await asyncio.to_thread(lambda: _l2(np.stack(list(self._model.embed(texts, batch_size=32)))))

    async def embed_query(self, text: str) -> np.ndarray:
        return await asyncio.to_thread(lambda: _l2(np.stack(list(self._model.query_embed([text]))))[0])


class GeminiEmbedder:
    name = "gemini"
    confident_cosine = 0.55

    def __init__(self, api_key: str, model: str, dim: int = 768) -> None:
        from google import genai

        self._client = genai.Client(api_key=api_key)
        self._model = model
        self.dim = dim
        self.name = f"gemini:{model}:{dim}"

    async def _embed(self, texts: list[str], task: str) -> np.ndarray:
        from google.genai import types

        out: list[list[float]] = []
        for i in range(0, len(texts), 100):
            res = await self._client.aio.models.embed_content(
                model=self._model,
                contents=texts[i : i + 100],
                config=types.EmbedContentConfig(task_type=task, output_dimensionality=self.dim),
            )
            out.extend(e.values for e in res.embeddings or [])
        return _l2(np.array(out, dtype=np.float32))

    async def embed_documents(self, texts: list[str]) -> np.ndarray:
        return await self._embed(texts, "RETRIEVAL_DOCUMENT") if texts else np.zeros((0, self.dim), np.float32)

    async def embed_query(self, text: str) -> np.ndarray:
        return (await self._embed([text], "RETRIEVAL_QUERY"))[0]


def build_embedder(settings: Settings | None = None) -> Embedder:
    s = settings or get_settings()
    if s.embedder == "gemini" and s.gemini_api_key:
        return GeminiEmbedder(s.gemini_api_key, s.gemini_embedding_model)
    if s.embedder == "fastembed":
        try:
            return FastEmbedEmbedder(s.fastembed_model, cache_dir=f"{s.data_dir}/models")
        except Exception as exc:  # modèle non téléchargeable (offline) ou lib absente
            logger.warning("fastembed indisponible (%s), fallback hashing embedder", exc)
    return HashingEmbedder()
