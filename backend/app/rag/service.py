"""Service RAG : ingestion (parse → chunk → enrich → embed → index) et recherche hybride."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.core.metrics import RAG_LATENCY
from app.db import session_factory
from app.models import Chunk, Document, DocumentStatus
from app.rag.chunking import chunk_text
from app.rag.embedding import Embedder, build_embedder, normalize
from app.rag.parsing import extract_text
from app.rag.retrieval import BM25Index, TTLCache, rrf, terms
from app.rag.store import MemoryVectorStore, QdrantVectorStore, VectorPoint, VectorStore

logger = logging.getLogger(__name__)


@dataclass
class RagHit:
    chunk_id: str
    document_id: str
    knowledge_base_id: str
    content: str
    score: float
    source: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id, "document_id": self.document_id, "content": self.content,
            "score": round(self.score, 4), "source": self.source, "metadata": self.metadata,
        }


@dataclass
class RagResult:
    query: str
    hits: list[RagHit]
    low_confidence: bool
    latency_ms: float


class RagService:
    def __init__(self, settings: Settings | None = None, embedder: Embedder | None = None, store: VectorStore | None = None) -> None:
        self.settings = settings or get_settings()
        self.embedder = embedder or build_embedder(self.settings)
        self.store = store or self._build_store()
        self._lex: dict[str, BM25Index] = {}
        self._lex_locks: dict[str, asyncio.Lock] = {}
        self._cache = TTLCache()

    def _build_store(self) -> VectorStore:
        s = self.settings
        collection = f"{s.qdrant_collection}_{self.embedder.name}_{self.embedder.dim}"
        if s.vector_store == "memory":
            return MemoryVectorStore()
        if s.vector_store == "qdrant" and s.qdrant_url:
            return QdrantVectorStore(collection, self.embedder.dim, url=s.qdrant_url, api_key=s.qdrant_api_key)
        path = Path(s.data_dir) / "qdrant"
        path.mkdir(parents=True, exist_ok=True)
        return QdrantVectorStore(collection, self.embedder.dim, path=str(path))

    # ------------------------------------------------------------------ ingestion
    async def ingest(self, document_id: uuid.UUID, filename: str, data: bytes) -> int:
        sf = session_factory()
        async with sf() as db:
            doc = await db.get(Document, document_id)
            if doc is None:
                return 0
            doc.status = DocumentStatus.processing.value
            await db.commit()
            org_id, kb_id = str(doc.organization_id), str(doc.knowledge_base_id)
        try:
            text = await asyncio.to_thread(extract_text, filename, data)
            if not text.strip():
                raise ValueError("document vide ou illisible")
            drafts = chunk_text(text)
            if self.settings.rag_enrich_with_llm:
                from app.llm.text import get_text_llm

                summaries = await asyncio.gather(*(get_text_llm().summarize_chunk(d.content) for d in drafts))
            else:
                summaries = [""] * len(drafts)
            vectors = await self.embedder.embed_documents([d.content for d in drafts])
            chunk_rows, points = [], []
            for d, summary, vec in zip(drafts, summaries, vectors):
                cid = uuid.uuid4()
                payload = {
                    "organization_id": org_id, "knowledge_base_id": kb_id, "document_id": str(document_id),
                    "chunk_id": str(cid), "source": filename, "content": d.content, "metadata": d.metadata,
                }
                points.append(VectorPoint(str(cid), vec, payload))
                chunk_rows.append(
                    Chunk(id=cid, organization_id=uuid.UUID(org_id), knowledge_base_id=uuid.UUID(kb_id), document_id=document_id,
                          content=d.content, summary=summary, embedding_id=str(cid), meta=d.metadata | {"source": filename})
                )
            await self.store.upsert(points)
            async with sf() as db:
                doc = await db.get(Document, document_id)
                assert doc is not None
                db.add_all(chunk_rows)
                doc.status = DocumentStatus.ready.value
                doc.chunk_count = len(chunk_rows)
                doc.error = None
                await db.commit()
            lex = await self._lexicon(org_id)
            for p in points:
                lex.add(p.id, p.payload["content"], p.payload)
            self._cache.invalidate_prefix(org_id)
            logger.info("document ingéré", extra={"extra_fields": {"document_id": str(document_id), "chunks": len(points)}})
            return len(points)
        except Exception as exc:
            logger.exception("échec ingestion document %s", document_id)
            async with sf() as db:
                doc = await db.get(Document, document_id)
                if doc is not None:
                    doc.status = DocumentStatus.failed.value
                    doc.error = str(exc)[:500]
                    await db.commit()
            return 0

    async def delete_document(self, org_id: str, document_id: str) -> None:
        await self.store.delete_document(org_id, document_id)
        lex = self._lex.get(org_id)
        if lex:
            for cid in [d.id for d in lex.docs.values() if d.payload.get("document_id") == document_id]:
                lex.remove(cid)
        self._cache.invalidate_prefix(org_id)

    async def _lexicon(self, org_id: str) -> BM25Index:
        if org_id in self._lex:
            return self._lex[org_id]
        lock = self._lex_locks.setdefault(org_id, asyncio.Lock())
        async with lock:
            if org_id in self._lex:
                return self._lex[org_id]
            index = BM25Index()
            async with session_factory()() as db:
                rows = await db.scalars(select(Chunk).where(Chunk.organization_id == uuid.UUID(org_id)))
                for c in rows:
                    index.add(str(c.id), c.content, {
                        "organization_id": org_id, "knowledge_base_id": str(c.knowledge_base_id), "document_id": str(c.document_id),
                        "chunk_id": str(c.id), "source": (c.meta or {}).get("source", ""), "content": c.content, "metadata": c.meta or {},
                    })
            self._lex[org_id] = index
            return index

    # ------------------------------------------------------------------ recherche
    async def search(self, org_id: str, query: str, kb_ids: list[str] | None = None, top_k: int | None = None) -> RagResult:
        t0 = time.perf_counter()
        top_k = top_k or self.settings.rag_top_k
        key = (org_id, tuple(sorted(kb_ids or [])), normalize(query).strip(), top_k)
        if (cached := self._cache.get(key)) is not None:
            return RagResult(query, cached[0], cached[1], (time.perf_counter() - t0) * 1000)

        qvec, lex = await asyncio.gather(self.embedder.embed_query(query), self._lexicon(org_id))
        dense = await self.store.search(org_id, qvec, top_k * 5, kb_ids)
        lexical = lex.search(query, top_k * 5, kb_ids)

        payloads: dict[str, dict[str, Any]] = {h.id: h.payload for h in dense}
        for doc_id, _, _ in lexical:
            payloads.setdefault(doc_id, lex.docs[doc_id].payload)
        fused = rrf([[h.id for h in dense], [d for d, _, _ in lexical]])
        ranked = sorted(fused.items(), key=lambda x: -x[1])[:top_k]

        max_dense = max((h.score for h in dense), default=0.0)
        q_terms = len(set(terms(query))) or 1
        best_lex_cov = max((m / q_terms for _, _, m in lexical), default=0.0)
        low_conf = not (max_dense >= self.embedder.confident_cosine or best_lex_cov >= 0.5)

        dense_scores = {h.id: h.score for h in dense}
        hits = [
            RagHit(
                chunk_id=cid, document_id=payloads[cid]["document_id"], knowledge_base_id=payloads[cid]["knowledge_base_id"],
                content=payloads[cid]["content"], score=score, source=payloads[cid].get("source", ""),
                metadata=(payloads[cid].get("metadata") or {}) | {"dense": round(dense_scores.get(cid, 0.0), 4)},
            )
            for cid, score in ranked
        ]
        self._cache.set(key, (hits, low_conf))
        elapsed = time.perf_counter() - t0
        RAG_LATENCY.observe(elapsed)
        return RagResult(query, hits, low_conf, elapsed * 1000)

    async def close(self) -> None:
        await self.store.close()


_rag: RagService | None = None


def get_rag() -> RagService:
    global _rag
    if _rag is None:
        _rag = RagService()
    return _rag


def set_rag(rag: RagService | None) -> None:
    global _rag
    _rag = rag
