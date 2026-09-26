"""Stores vectoriels : Qdrant (embarqué local ou serveur) et mémoire (tests)."""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np


@dataclass
class VectorPoint:
    id: str
    vector: np.ndarray
    payload: dict[str, Any]


@dataclass
class VectorHit:
    id: str
    score: float
    payload: dict[str, Any]


class VectorStore(Protocol):
    async def upsert(self, points: list[VectorPoint]) -> None: ...
    async def search(self, org_id: str, vector: np.ndarray, top_k: int, kb_ids: list[str] | None = None) -> list[VectorHit]: ...
    async def delete_document(self, org_id: str, document_id: str) -> None: ...
    async def close(self) -> None: ...


class MemoryVectorStore:
    def __init__(self) -> None:
        self._points: dict[str, VectorPoint] = {}

    async def upsert(self, points: list[VectorPoint]) -> None:
        for p in points:
            self._points[p.id] = p

    async def search(self, org_id: str, vector: np.ndarray, top_k: int, kb_ids: list[str] | None = None) -> list[VectorHit]:
        cands = [p for p in self._points.values() if p.payload["organization_id"] == org_id and (not kb_ids or p.payload["knowledge_base_id"] in kb_ids)]
        if not cands:
            return []
        scores = np.stack([p.vector for p in cands]) @ vector
        order = np.argsort(-scores)[:top_k]
        return [VectorHit(cands[i].id, float(scores[i]), cands[i].payload) for i in order]

    async def delete_document(self, org_id: str, document_id: str) -> None:
        for pid in [k for k, p in self._points.items() if p.payload["document_id"] == document_id and p.payload["organization_id"] == org_id]:
            del self._points[pid]

    async def close(self) -> None:
        return None


class QdrantVectorStore:
    """Qdrant : `path` = mode embarqué (aucun serveur requis), `url` = cluster."""

    def __init__(self, collection: str, dim: int, *, path: str | None = None, url: str | None = None, api_key: str | None = None) -> None:
        from qdrant_client import AsyncQdrantClient

        self._client = AsyncQdrantClient(path=path) if path else AsyncQdrantClient(url=url, api_key=api_key)
        self._collection = re.sub(r"[^a-zA-Z0-9_]", "_", collection)
        self._dim = dim
        self._ready = False
        self._lock = asyncio.Lock()

    async def _ensure(self) -> None:
        if self._ready:
            return
        async with self._lock:
            if self._ready:
                return
            from qdrant_client import models

            import warnings

            if not await self._client.collection_exists(self._collection):
                await self._client.create_collection(
                    self._collection, vectors_config=models.VectorParams(size=self._dim, distance=models.Distance.COSINE)
                )
                for field in ("organization_id", "knowledge_base_id", "document_id"):
                    try:
                        with warnings.catch_warnings():
                            warnings.simplefilter("ignore")
                            await self._client.create_payload_index(self._collection, field, models.PayloadSchemaType.KEYWORD)
                    except Exception:  # non supporté en mode local
                        pass
            self._ready = True

    async def upsert(self, points: list[VectorPoint]) -> None:
        from qdrant_client import models

        await self._ensure()
        for i in range(0, len(points), 128):
            batch = points[i : i + 128]
            await self._client.upsert(
                self._collection,
                points=[models.PointStruct(id=p.id, vector=p.vector.tolist(), payload=p.payload) for p in batch],
            )

    def _filter(self, org_id: str, kb_ids: list[str] | None = None, document_id: str | None = None) -> Any:
        from qdrant_client import models

        must: list[Any] = [models.FieldCondition(key="organization_id", match=models.MatchValue(value=org_id))]
        if kb_ids:
            must.append(models.FieldCondition(key="knowledge_base_id", match=models.MatchAny(any=kb_ids)))
        if document_id:
            must.append(models.FieldCondition(key="document_id", match=models.MatchValue(value=document_id)))
        return models.Filter(must=must)

    async def search(self, org_id: str, vector: np.ndarray, top_k: int, kb_ids: list[str] | None = None) -> list[VectorHit]:
        await self._ensure()
        res = await self._client.query_points(
            self._collection, query=vector.tolist(), query_filter=self._filter(org_id, kb_ids), limit=top_k, with_payload=True
        )
        return [VectorHit(str(p.id), float(p.score), dict(p.payload or {})) for p in res.points]

    async def delete_document(self, org_id: str, document_id: str) -> None:
        from qdrant_client import models

        await self._ensure()
        await self._client.delete(self._collection, points_selector=models.FilterSelector(filter=self._filter(org_id, document_id=document_id)))

    async def close(self) -> None:
        await self._client.close()
