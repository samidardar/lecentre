"""WebSocket temps réel pour le dashboard.

    /ws/calls              tous les événements call.* de l'organisation
    /ws/campaigns/{id}     événements d'une campagne (progress, status, appels de la campagne)
    /ws/analytics          analytics.updated (+ snapshot initial)

Auth : `?token=<access_token>` (les navigateurs ne peuvent pas poser d'en-tête Authorization sur un WS).
Heartbeat : {"event":"ping"} toutes les 20 s ; le client peut envoyer {"type":"ping"} → {"event":"pong"}.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from dataclasses import dataclass, field

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from app.agents.analytics import analytics_agent
from app.api.deps import APIError, principal_from_token
from app.core import events as ev
from app.db import session_factory
from app.models import Campaign

logger = logging.getLogger(__name__)
router = APIRouter(tags=["websocket"])


@dataclass(eq=False)
class Client:
    ws: WebSocket
    org_id: str
    channel: str  # calls | campaign | analytics
    campaign_id: str | None = None
    queue: asyncio.Queue[str] = field(default_factory=lambda: asyncio.Queue(1000))

    def wants(self, e: ev.Event) -> bool:
        if e.organization_id != self.org_id:
            return False
        if self.channel == "calls":
            return e.event.startswith("call.")
        if self.channel == "campaign":
            return e.campaign_id == self.campaign_id
        return e.event == ev.ANALYTICS_UPDATED


class Hub:
    def __init__(self) -> None:
        self.clients: set[Client] = set()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._fanout())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _fanout(self) -> None:
        async for e in ev.get_bus().subscribe():
            if not self.clients:
                continue
            data = None
            for c in list(self.clients):
                if c.wants(e):
                    data = data or e.model_dump_json()
                    try:
                        c.queue.put_nowait(data)
                    except asyncio.QueueFull:
                        pass  # client lent : on perd des événements plutôt que de bloquer


hub = Hub()


async def _serve(ws: WebSocket, token: str, channel: str, campaign_id: str | None = None) -> None:
    await ws.accept()
    try:
        async with session_factory()() as db:
            p = await principal_from_token(token, db)
            if campaign_id:
                camp = await db.get(Campaign, uuid.UUID(campaign_id))
                if camp is None or camp.organization_id != p.org_id:
                    raise APIError(404, "not_found", "Campagne introuvable")
    except (APIError, ValueError) as exc:
        detail = exc.detail if isinstance(exc, APIError) else {"code": "bad_request", "message": str(exc)}
        await ws.send_text(json.dumps({"event": "error", "payload": detail}))
        await ws.close(code=4401)
        return
    client = Client(ws, str(p.org_id), channel, campaign_id)
    hub.clients.add(client)
    await ws.send_text(json.dumps({"event": "connected", "payload": {"channel": channel, "organization_id": client.org_id}}))
    if channel == "analytics":
        await ws.send_text(ev.Event(event=ev.ANALYTICS_UPDATED, organization_id=client.org_id,
                                    payload=analytics_agent.snapshot(client.org_id)).model_dump_json())

    async def sender() -> None:
        while True:
            try:
                msg = await asyncio.wait_for(client.queue.get(), 20)
            except asyncio.TimeoutError:
                msg = json.dumps({"event": "ping"})
            await ws.send_text(msg)

    async def receiver() -> None:
        while True:
            raw = await ws.receive_text()
            with contextlib.suppress(Exception):
                if json.loads(raw).get("type") == "ping":
                    await client.queue.put(json.dumps({"event": "pong"}))

    tasks = [asyncio.create_task(sender()), asyncio.create_task(receiver())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        pass
    finally:
        for t in tasks:
            t.cancel()
        hub.clients.discard(client)


@router.websocket("/ws/calls")
async def ws_calls(ws: WebSocket, token: str = Query(...)) -> None:
    await _serve(ws, token, "calls")


@router.websocket("/ws/campaigns/{campaign_id}")
async def ws_campaign(ws: WebSocket, campaign_id: str, token: str = Query(...)) -> None:
    await _serve(ws, token, "campaign", campaign_id)


@router.websocket("/ws/analytics")
async def ws_analytics(ws: WebSocket, token: str = Query(...)) -> None:
    await _serve(ws, token, "analytics")


@router.websocket("/ws/webcall")
async def ws_webcall(ws: WebSocket, token: str = Query(...), phone_number_id: str | None = None,
                     campaign_id: str | None = None) -> None:
    """Appel vocal depuis le navigateur : même pipeline que le téléphone (agents, RAG, supervision, analytics)."""
    from app.models import Contact, PhoneNumber
    from app.services.call_manager import get_call_manager
    from app.voice.transport import BrowserTransport

    await ws.accept()
    try:
        async with session_factory()() as db:
            p = await principal_from_token(token, db)
            direction, to_number, contact_id, pn_id, camp_id = "inbound", "web", None, None, None
            if campaign_id:
                camp = await db.get(Campaign, uuid.UUID(campaign_id))
                if camp is None or camp.organization_id != p.org_id:
                    raise APIError(404, "not_found", "Campagne introuvable")
                from sqlalchemy import select

                contact = await db.scalar(select(Contact).where(Contact.campaign_id == camp.id).limit(1))
                direction, camp_id, contact_id = "outbound", camp.id, contact.id if contact else None
            else:
                from sqlalchemy import select

                stmt = select(PhoneNumber).where(PhoneNumber.organization_id == p.org_id)
                if phone_number_id:
                    stmt = stmt.where(PhoneNumber.id == uuid.UUID(phone_number_id))
                pn = await db.scalar(stmt.limit(1))
                if pn is not None:
                    pn_id, to_number = pn.id, pn.number
    except (APIError, ValueError) as exc:
        detail = exc.detail if isinstance(exc, APIError) else {"code": "bad_request", "message": str(exc)}
        await ws.send_text(json.dumps({"type": "error", "payload": detail}))
        await ws.close(code=4401)
        return
    manager = get_call_manager()
    call = await manager.create_call(organization_id=p.org_id, direction=direction, from_number="navigateur", to_number=to_number,
                                     campaign_id=camp_id, contact_id=contact_id, phone_number_id=pn_id)
    await ws.send_text(json.dumps({"type": "call", "call_id": str(call.id), "direction": direction}))
    await manager.start_session(call.id, BrowserTransport(ws))
