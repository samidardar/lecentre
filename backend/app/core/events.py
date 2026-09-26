"""Bus d'événements temps réel.

Tous les événements (appel, campagne, analytics) transitent par ce bus :
Call Session → bus → Analytics Agent / WebSocket hub → frontend.
`MemoryBus` en mono-process, `RedisBus` (pub/sub) dès que plusieurs process (api + worker).
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Protocol

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Catalogue stable des événements (contrat frontend, cf. docs/WEBSOCKET_EVENTS.md)
CALL_STARTED = "call.started"
CALL_RINGING = "call.ringing"
CALL_ANSWERED = "call.answered"
CALL_UPDATED = "call.updated"
CALL_TRANSCRIPT_PARTIAL = "call.transcript_partial"
CALL_TRANSCRIPT_FINAL = "call.transcript_final"
CALL_INTENT_DETECTED = "call.intent_detected"
CALL_RESPONSE_GENERATED = "call.response_generated"
CALL_RESPONSE_VALIDATED = "call.response_validated"
CALL_TTS_STARTED = "call.tts_started"
CALL_USER_INTERRUPTED = "call.user_interrupted"
CALL_TOOL_CALLED = "call.tool_called"
CALL_AGENT_DECISION = "call.agent_decision"
CALL_LATENCY = "call.latency"
CALL_TRANSFERRED = "call.transferred"
CALL_COMPLETED = "call.completed"
CALL_FAILED = "call.failed"
CAMPAIGN_PROGRESS = "campaign.progress"
CAMPAIGN_STATUS = "campaign.status"
ANALYTICS_UPDATED = "analytics.updated"


class Event(BaseModel):
    event: str
    organization_id: str
    call_id: str | None = None
    campaign_id: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    payload: dict[str, Any] = Field(default_factory=dict)
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)


class EventBus(Protocol):
    async def publish(self, event: Event) -> None: ...
    def subscribe(self) -> AsyncIterator[Event]: ...
    async def close(self) -> None: ...


class MemoryBus:
    """Fan-out non bloquant : un subscriber lent perd des événements plutôt que de freiner les appels."""

    def __init__(self, max_queue: int = 10_000) -> None:
        self._subs: set[asyncio.Queue[Event]] = set()
        self._max_queue = max_queue

    async def publish(self, event: Event) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("event subscriber saturated, dropping %s", event.event)

    async def subscribe(self) -> AsyncIterator[Event]:  # type: ignore[override]
        q: asyncio.Queue[Event] = asyncio.Queue(self._max_queue)
        self._subs.add(q)
        try:
            while True:
                yield await q.get()
        finally:
            self._subs.discard(q)

    async def close(self) -> None:
        self._subs.clear()


class RedisBus:
    CHANNEL = "callwiz:events"

    def __init__(self, url: str) -> None:
        import redis.asyncio as redis  # dépendance optionnelle [prod]

        self._redis = redis.from_url(url, decode_responses=True)

    async def publish(self, event: Event) -> None:
        await self._redis.publish(self.CHANNEL, event.model_dump_json())

    async def subscribe(self) -> AsyncIterator[Event]:  # type: ignore[override]
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(self.CHANNEL)
        try:
            async for msg in pubsub.listen():
                if msg.get("type") == "message":
                    yield Event.model_validate(json.loads(msg["data"]))
        finally:
            await pubsub.unsubscribe(self.CHANNEL)
            await pubsub.aclose()

    async def close(self) -> None:
        await self._redis.aclose()


_bus: EventBus | None = None


def get_bus() -> EventBus:
    global _bus
    if _bus is None:
        from app.core.config import get_settings

        url = get_settings().redis_url
        _bus = RedisBus(url) if url else MemoryBus()
    return _bus


def set_bus(bus: EventBus | None) -> None:
    global _bus
    _bus = bus


async def emit(event: str, organization_id: Any, *, call_id: Any = None, campaign_id: Any = None, **payload: Any) -> Event:
    ev = Event(
        event=event,
        organization_id=str(organization_id),
        call_id=str(call_id) if call_id else None,
        campaign_id=str(campaign_id) if campaign_id else None,
        payload=payload,
    )
    await get_bus().publish(ev)
    return ev
