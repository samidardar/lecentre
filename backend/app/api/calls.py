from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter
from sqlalchemy import select

from app.api.deps import DB, APIError, CurrentPrincipal, Paging, get_owned, paginate
from app.core.config import get_settings
from app.models import AgentDecision, Call, CallEvent, Campaign, Contact, PhoneNumber
from app.schemas import CallDebug, CallDetail, CallEventOut, CallOut, OutboundTestIn, Page, SimulateCallIn
from app.services.call_manager import get_call_manager
from app.voice.transport import SimulatedCallerTransport

router = APIRouter(prefix="/calls", tags=["calls"])


@router.get("", response_model=Page[CallOut])
async def list_calls(p: CurrentPrincipal, db: DB, paging: Paging, status: str | None = None, direction: str | None = None,
                     campaign_id: uuid.UUID | None = None, since: datetime | None = None) -> dict:
    stmt = select(Call).where(Call.organization_id == p.org_id).order_by(Call.created_at.desc())
    if status:
        stmt = stmt.where(Call.status == status)
    if direction:
        stmt = stmt.where(Call.direction == direction)
    if campaign_id:
        stmt = stmt.where(Call.campaign_id == campaign_id)
    if since:
        stmt = stmt.where(Call.created_at >= since)
    items, total = await paginate(db, stmt, paging)
    return {"items": items, "total": total, "limit": paging.limit, "offset": paging.offset}


@router.get("/{call_id}", response_model=CallDetail)
async def get_call(call_id: uuid.UUID, p: CurrentPrincipal, db: DB) -> Call:
    return await get_owned(db, Call, call_id, p.org_id, "Appel")


@router.get("/{call_id}/transcript")
async def get_transcript(call_id: uuid.UUID, p: CurrentPrincipal, db: DB) -> dict:
    call = await get_owned(db, Call, call_id, p.org_id, "Appel")
    return {"call_id": str(call.id), "transcript": call.transcript or [], "summary": call.summary}


@router.get("/{call_id}/events", response_model=list[CallEventOut])
async def get_events(call_id: uuid.UUID, p: CurrentPrincipal, db: DB) -> list[CallEvent]:
    await get_owned(db, Call, call_id, p.org_id, "Appel")
    return list((await db.scalars(select(CallEvent).where(CallEvent.call_id == call_id).order_by(CallEvent.created_at))).all())


@router.get("/{call_id}/debug", response_model=CallDebug)
async def get_debug(call_id: uuid.UUID, p: CurrentPrincipal, db: DB) -> dict:
    call = await get_owned(db, Call, call_id, p.org_id, "Appel")
    events = list((await db.scalars(select(CallEvent).where(CallEvent.call_id == call_id).order_by(CallEvent.created_at))).all())
    decisions = list((await db.scalars(select(AgentDecision).where(AgentDecision.call_id == call_id).order_by(AgentDecision.created_at))).all())
    rag_chunks = [
        {"at": e.created_at.isoformat(), "query": (e.payload.get("args") or {}).get("query"), "chunk_ids": e.payload.get("rag_chunks"),
         "result": e.payload.get("result_preview")}
        for e in events if e.type == "call.tool_called" and e.payload.get("tool") == "search_knowledge_base"
    ]
    return {"call": call, "events": events, "decisions": decisions, "rag_chunks": rag_chunks}


@router.post("/{call_id}/hangup", status_code=202)
async def hangup(call_id: uuid.UUID, p: CurrentPrincipal, db: DB) -> dict:
    await get_owned(db, Call, call_id, p.org_id, "Appel")
    ended = await get_call_manager().end_call(call_id)
    return {"ended": ended}


@router.post("/simulate", response_model=CallOut, status_code=202)
async def simulate_call(body: SimulateCallIn, p: CurrentPrincipal, db: DB) -> Call:
    """Appel simulé de bout en bout (sans téléphone) : l'appelant parle en texte, l'IA (mock ou Gemini réel) répond.
    Idéal pour tester un script, une base de connaissances ou le dashboard temps réel."""
    to_number, from_number, contact_id, pn_id = "+33100000000", "+33600000000", None, None
    if body.phone_number_id:
        pn = await get_owned(db, PhoneNumber, body.phone_number_id, p.org_id, "Numéro")
        to_number, pn_id = pn.number, pn.id
    if body.direction == "outbound":
        if not body.campaign_id:
            raise APIError(422, "campaign_required", "campaign_id requis pour une simulation sortante")
        await get_owned(db, Campaign, body.campaign_id, p.org_id, "Campagne")
        contact = await db.scalar(select(Contact).where(Contact.campaign_id == body.campaign_id).limit(1))
        contact_id = contact.id if contact else None
        from_number, to_number = to_number, (contact.phone if contact else from_number)
    manager = get_call_manager()
    call = await manager.create_call(organization_id=p.org_id, direction=body.direction, from_number=from_number, to_number=to_number,
                                     campaign_id=body.campaign_id, contact_id=contact_id, phone_number_id=pn_id)
    manager.start_session(call.id, SimulatedCallerTransport(body.caller_script, think_time=0.4))
    return call


@router.post("/test-outbound", response_model=CallOut, status_code=202)
async def test_outbound(body: OutboundTestIn, p: CurrentPrincipal, db: DB) -> Call:
    """Appel réel sortant vers un numéro (Twilio) — idéal pour écouter l'agent sur son propre téléphone."""
    if body.campaign_id:
        await get_owned(db, Campaign, body.campaign_id, p.org_id, "Campagne")
    manager = get_call_manager()
    call = await manager.create_call(organization_id=p.org_id, direction="outbound", from_number=get_settings().twilio_default_from or "",
                                     to_number=body.to, campaign_id=body.campaign_id)
    await manager.dial(call)
    return call
