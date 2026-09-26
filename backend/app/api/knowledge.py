from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, File, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.deps import DB, APIError, CurrentPrincipal, get_owned
from app.models import Document, KnowledgeBase
from app.rag.parsing import source_type_for
from app.rag.service import get_rag
from app.schemas import (
    DocumentOut, KnowledgeBaseCreate, KnowledgeBaseDetail, KnowledgeBaseOut, RetrievedChunk, TestQueryIn, TestQueryOut, TextDocumentIn,
)

router = APIRouter(tags=["knowledge"])
_ingest_tasks: set[asyncio.Task[int]] = set()
MAX_UPLOAD = 25 * 1024 * 1024


def _schedule_ingest(doc_id: uuid.UUID, filename: str, data: bytes) -> None:
    task = asyncio.create_task(get_rag().ingest(doc_id, filename, data))
    _ingest_tasks.add(task)
    task.add_done_callback(_ingest_tasks.discard)


@router.get("/knowledge-bases", response_model=list[KnowledgeBaseOut])
async def list_kbs(p: CurrentPrincipal, db: DB) -> list[KnowledgeBase]:
    return list((await db.scalars(select(KnowledgeBase).where(KnowledgeBase.organization_id == p.org_id).order_by(KnowledgeBase.created_at))).all())


@router.post("/knowledge-bases", response_model=KnowledgeBaseOut, status_code=201)
async def create_kb(body: KnowledgeBaseCreate, p: CurrentPrincipal, db: DB) -> KnowledgeBase:
    kb = KnowledgeBase(organization_id=p.org_id, **body.model_dump())
    db.add(kb)
    await db.commit()
    return kb


@router.get("/knowledge-bases/{kb_id}", response_model=KnowledgeBaseDetail)
async def get_kb(kb_id: uuid.UUID, p: CurrentPrincipal, db: DB) -> KnowledgeBase:
    kb = await db.scalar(select(KnowledgeBase).options(selectinload(KnowledgeBase.documents)).where(KnowledgeBase.id == kb_id))
    if kb is None or kb.organization_id != p.org_id:
        raise APIError(404, "not_found", "Base de connaissances introuvable")
    return kb


async def _create_document(db: DB, p: CurrentPrincipal, kb_id: uuid.UUID, filename: str, data: bytes) -> Document:
    await get_owned(db, KnowledgeBase, kb_id, p.org_id, "Base de connaissances")
    try:
        kind = source_type_for(filename)
    except ValueError as exc:
        raise APIError(415, "unsupported_format", str(exc))
    if not data:
        raise APIError(422, "empty_file", "Fichier vide")
    if len(data) > MAX_UPLOAD:
        raise APIError(413, "file_too_large", "Fichier limité à 25 Mo")
    doc = Document(organization_id=p.org_id, knowledge_base_id=kb_id, filename=filename, source_type=kind)
    db.add(doc)
    await db.commit()
    _schedule_ingest(doc.id, filename, data)
    return doc


@router.post("/knowledge-bases/{kb_id}/documents", response_model=DocumentOut, status_code=202)
async def upload_document(kb_id: uuid.UUID, p: CurrentPrincipal, db: DB, file: UploadFile = File(...)) -> Document:
    return await _create_document(db, p, kb_id, file.filename or "document.txt", await file.read())


@router.post("/knowledge-bases/{kb_id}/documents/text", response_model=DocumentOut, status_code=202)
async def add_text_document(kb_id: uuid.UUID, body: TextDocumentIn, p: CurrentPrincipal, db: DB) -> Document:
    """Ajout direct de texte (FAQ collée dans l'UI, script, politique…)."""
    return await _create_document(db, p, kb_id, body.filename, body.content.encode("utf-8"))


@router.get("/documents/{doc_id}/status", response_model=DocumentOut)
async def document_status(doc_id: uuid.UUID, p: CurrentPrincipal, db: DB) -> Document:
    return await get_owned(db, Document, doc_id, p.org_id, "Document")


@router.delete("/documents/{doc_id}", status_code=204)
async def delete_document(doc_id: uuid.UUID, p: CurrentPrincipal, db: DB) -> None:
    doc = await get_owned(db, Document, doc_id, p.org_id, "Document")
    await get_rag().delete_document(str(p.org_id), str(doc_id))
    await db.delete(doc)
    await db.commit()


@router.post("/knowledge-bases/{kb_id}/test-query", response_model=TestQueryOut)
async def test_query(kb_id: uuid.UUID, body: TestQueryIn, p: CurrentPrincipal, db: DB) -> TestQueryOut:
    await get_owned(db, KnowledgeBase, kb_id, p.org_id, "Base de connaissances")
    res = await get_rag().search(str(p.org_id), body.query, [str(kb_id)], top_k=body.top_k)
    return TestQueryOut(
        query=body.query, low_confidence=res.low_confidence, latency_ms=round(res.latency_ms, 2),
        chunks=[RetrievedChunk(**h.as_dict()) for h in res.hits],
    )
