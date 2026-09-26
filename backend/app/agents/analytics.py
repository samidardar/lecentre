"""Analytics Agent.

1. Post-appel (nœuds du graphe `post_call`) : analyse LLM (intent, sentiment, outcome, résumé), coût, persistance, webhook.
2. Temps réel : consomme le bus d'événements, persiste CallEvent/AgentDecision par lots (jamais sur le chemin de l'appel),
   maintient des compteurs par organisation et pousse `analytics.updated` (throttlé) vers le dashboard.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from app.core import events as ev
from app.core.config import get_settings
from app.db import session_factory
from app.llm.text import CallAnalysis, get_text_llm
from app.models import AgentDecision, CallEvent

logger = logging.getLogger(__name__)

SKIP_PERSIST = {ev.CALL_TRANSCRIPT_PARTIAL, ev.ANALYTICS_UPDATED, ev.CAMPAIGN_PROGRESS}


async def analyze_transcript(transcript: list[dict[str, Any]], context: dict[str, Any]) -> CallAnalysis:
    if not any(t.get("role") == "user" for t in transcript):
        return CallAnalysis(summary="Aucun échange avec l'interlocuteur.", intent="other", sentiment=0.5,
                            outcome=context.get("outcome") or "failed")
    return await get_text_llm().analyze_call(transcript, context)


async def send_webhook(url: str, payload: dict[str, Any]) -> None:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(url, json=payload)
    except Exception as exc:
        logger.warning("webhook campagne en échec: %s", exc)


@dataclass
class OrgCounters:
    day: str = ""
    active: dict[str, int] = field(default_factory=lambda: {"inbound": 0, "outbound": 0})
    calls_today: int = 0
    completed_today: int = 0
    failed_today: int = 0
    cost_today: float = 0.0
    latencies: deque[float] = field(default_factory=lambda: deque(maxlen=500))
    dirty: bool = False
    last_push: float = 0.0

    def roll(self) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        if self.day != today:
            self.day, self.calls_today, self.completed_today, self.failed_today, self.cost_today = today, 0, 0, 0, 0.0


class AnalyticsAgent:
    push_interval_s = 0.5
    flush_interval_s = 0.25

    def __init__(self) -> None:
        self.counters: dict[str, OrgCounters] = defaultdict(OrgCounters)
        self._buffer: list[ev.Event] = []
        self._tasks: list[asyncio.Task[None]] = []

    def snapshot(self, org_id: str) -> dict[str, Any]:
        c = self.counters[org_id]
        c.roll()
        cap = get_settings().global_max_concurrent_calls
        active = sum(c.active.values())
        lat = sorted(c.latencies)
        return {
            "active_calls": active, "active_by_direction": dict(c.active), "calls_today": c.calls_today,
            "completed_today": c.completed_today, "failed_today": c.failed_today, "cost_today": round(c.cost_today, 4),
            "avg_latency_ms": round(sum(lat) / len(lat), 1) if lat else None, "capacity": cap,
            "utilization": round(active / cap, 3) if cap else 0.0,
        }

    def _apply(self, e: ev.Event) -> None:
        c = self.counters[e.organization_id]
        c.roll()
        direction = e.payload.get("direction", "outbound")
        if e.event == ev.CALL_STARTED:
            c.calls_today += 1
        elif e.event == ev.CALL_ANSWERED:
            c.active[direction] = c.active.get(direction, 0) + 1
        elif e.event in (ev.CALL_COMPLETED, ev.CALL_FAILED, ev.CALL_TRANSFERRED):
            if e.payload.get("was_active"):
                c.active[direction] = max(0, c.active.get(direction, 0) - 1)
            if e.event == ev.CALL_FAILED:
                c.failed_today += 1
            elif e.event == ev.CALL_COMPLETED:
                c.completed_today += 1
            c.cost_today += float(e.payload.get("cost_estimate") or 0.0)
        elif e.event == ev.CALL_LATENCY and e.payload.get("voice_to_voice_ms") is not None:
            c.latencies.append(float(e.payload["voice_to_voice_ms"]))
        else:
            return
        c.dirty = True

    async def run(self) -> None:
        self._tasks = [asyncio.create_task(self._consume()), asyncio.create_task(self._flusher()), asyncio.create_task(self._pusher())]
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _consume(self) -> None:
        async for e in ev.get_bus().subscribe():
            try:
                self._apply(e)
                if e.call_id and e.event not in SKIP_PERSIST:
                    self._buffer.append(e)
            except Exception:
                logger.exception("analytics: événement invalide")

    async def _flusher(self) -> None:
        while True:
            await asyncio.sleep(self.flush_interval_s)
            await self.flush()

    async def flush(self) -> None:
        if not self._buffer:
            return
        batch, self._buffer = self._buffer, []
        rows: list[Any] = []
        for e in batch:
            try:
                call_id, org_id = uuid.UUID(e.call_id or ""), uuid.UUID(e.organization_id)
            except ValueError:
                continue
            if e.event == ev.CALL_AGENT_DECISION:
                p = e.payload
                rows.append(AgentDecision(call_id=call_id, organization_id=org_id, agent=p.get("agent", "supervisor"),
                                          decision=p.get("decision", ""), confidence=float(p.get("confidence", 1.0)),
                                          reason=p.get("reason", ""), data=p.get("data", {}), created_at=e.timestamp))
            rows.append(CallEvent(call_id=call_id, organization_id=org_id, type=e.event, payload=e.payload, created_at=e.timestamp))
        try:
            async with session_factory()() as db:
                db.add_all(rows)
                await db.commit()
        except Exception:
            logger.exception("analytics: échec persistance de %d événements", len(rows))

    async def _pusher(self) -> None:
        while True:
            await asyncio.sleep(self.push_interval_s)
            now = time.monotonic()
            for org_id, c in list(self.counters.items()):
                if c.dirty and now - c.last_push >= self.push_interval_s:
                    c.dirty, c.last_push = False, now
                    await ev.emit(ev.ANALYTICS_UPDATED, org_id, **self.snapshot(org_id))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await self.flush()


analytics_agent = AnalyticsAgent()
