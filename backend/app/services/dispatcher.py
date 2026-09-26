"""Dispatcher de campagnes sortantes avec backpressure.

- plafond global (GLOBAL_MAX_CONCURRENT_CALLS, compté en base → valable multi-process) ;
- plafond par campagne (`max_concurrency`) ;
- débit télécom (token bucket, appels/seconde) ;
- fenêtre horaire dans le fuseau de l'organisation ;
- relances avec délai (`retry_delay_minutes`, `max_attempts`) ;
- pause / stop immédiats (les appels en cours se terminent normalement) ;
- état reconstructible : au redémarrage, les campagnes `running` reprennent, les contacts `calling` orphelins repassent en `retry`.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select, update

from app.core import events as ev
from app.core.config import get_settings
from app.db import session_factory
from app.models import Call, CallStatus, Campaign, CampaignStatus, Contact, ContactStatus, Organization
from app.services.call_manager import ACTIVE_STATUSES, CallManager

logger = logging.getLogger(__name__)

RETRYABLE = {CallStatus.no_answer.value, CallStatus.busy.value, CallStatus.voicemail.value, CallStatus.failed.value}
RETRY_OUTCOMES = {"callback", "no_answer", "voicemail"}


class TokenBucket:
    def __init__(self, rate: float, burst: int | None = None) -> None:
        self.rate = rate
        self.capacity = burst or max(1, int(rate))
        self.tokens = float(self.capacity)
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def take(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep((1 - self.tokens) / self.rate)


def in_schedule(schedule: dict[str, Any], tz: str, now: datetime | None = None) -> bool:
    if not schedule:
        return True
    local = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(tz))
    if local.weekday() not in schedule.get("days", [0, 1, 2, 3, 4, 5, 6]):
        return False
    hm = local.strftime("%H:%M")
    return schedule.get("start", "00:00") <= hm < schedule.get("end", "23:59")


class CampaignDispatcher:
    poll_interval_s = 0.5

    def __init__(self, calls: CallManager, *, respect_schedule: bool = True) -> None:
        self.calls = calls
        self.settings = get_settings()
        self.respect_schedule = respect_schedule
        self._runners: dict[uuid.UUID, asyncio.Task[None]] = {}
        self._active: dict[uuid.UUID, set[uuid.UUID]] = {}  # campaign → call ids en cours (ce process)
        self._call_to_contact: dict[uuid.UUID, tuple[uuid.UUID, uuid.UUID]] = {}
        self._bucket = TokenBucket(self.settings.telephony_calls_per_second, burst=max(1, int(self.settings.telephony_calls_per_second * 2)))
        self._progress_last: dict[uuid.UUID, float] = {}
        calls.on_finished(self._on_call_finished)

    # ----------------------------------------------------------------- contrôle
    async def recover(self) -> None:
        async with session_factory()() as db:
            await db.execute(update(Contact).where(Contact.status == ContactStatus.calling.value).values(status=ContactStatus.retry.value))
            await db.execute(update(Call).where(Call.status.in_(ACTIVE_STATUSES)).values(status=CallStatus.failed.value, error="interrompu par redémarrage"))
            running = (await db.scalars(select(Campaign.id).where(Campaign.status == CampaignStatus.running.value))).all()
            await db.commit()
        for cid in running:
            self.ensure_running(cid)

    def ensure_running(self, campaign_id: uuid.UUID) -> None:
        task = self._runners.get(campaign_id)
        if task is None or task.done():
            self._runners[campaign_id] = asyncio.create_task(self._run(campaign_id), name=f"campaign-{campaign_id}")

    async def start(self, campaign_id: uuid.UUID) -> None:
        async with session_factory()() as db:
            camp = await db.get(Campaign, campaign_id)
            if camp is None:
                raise LookupError("campaign")
            camp.status = CampaignStatus.running.value
            camp.started_at = camp.started_at or datetime.now(timezone.utc)
            camp.completed_at = None
            org_id = camp.organization_id
            await db.commit()
        await ev.emit(ev.CAMPAIGN_STATUS, org_id, campaign_id=campaign_id, status="running")
        self.ensure_running(campaign_id)

    async def set_status(self, campaign_id: uuid.UUID, status: CampaignStatus) -> None:
        async with session_factory()() as db:
            camp = await db.get(Campaign, campaign_id)
            if camp is None:
                raise LookupError("campaign")
            camp.status = status.value
            org_id = camp.organization_id
            await db.commit()
        await ev.emit(ev.CAMPAIGN_STATUS, org_id, campaign_id=campaign_id, status=status.value)

    def active_calls(self, campaign_id: uuid.UUID) -> int:
        return len(self._active.get(campaign_id, ()))

    async def shutdown(self) -> None:
        for t in self._runners.values():
            t.cancel()
        await asyncio.gather(*self._runners.values(), return_exceptions=True)

    # ----------------------------------------------------------------- boucle
    async def _run(self, campaign_id: uuid.UUID) -> None:
        active = self._active.setdefault(campaign_id, set())
        try:
            while True:
                async with session_factory()() as db:
                    camp = await db.get(Campaign, campaign_id)
                    if camp is None or camp.status != CampaignStatus.running.value:
                        return
                    org = await db.get(Organization, camp.organization_id)
                    tz = org.timezone if org else "Europe/Paris"
                    schedule, max_conc, org_id = dict(camp.schedule or {}), camp.max_concurrency, camp.organization_id

                if self.respect_schedule and not in_schedule(schedule, tz):
                    await asyncio.sleep(30)
                    continue

                free_campaign = max_conc - len(active)
                free_global = self.settings.global_max_concurrent_calls - await self.calls.global_active()
                slots = min(free_campaign, free_global)
                if slots > 0:
                    launched = await self._launch_batch(campaign_id, org_id, slots)
                    if launched == 0 and not active and await self._is_exhausted(campaign_id):
                        await self._complete(campaign_id, org_id)
                        return
                await self._maybe_progress(campaign_id, org_id)
                await asyncio.sleep(self.poll_interval_s)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("dispatcher campagne %s en échec", campaign_id)
            await self.set_status(campaign_id, CampaignStatus.failed)

    async def _launch_batch(self, campaign_id: uuid.UUID, org_id: uuid.UUID, slots: int) -> int:
        now = datetime.now(timezone.utc)
        async with session_factory()() as db:
            camp = await db.get(Campaign, campaign_id)
            assert camp is not None
            contacts = (await db.scalars(
                select(Contact).where(
                    Contact.campaign_id == campaign_id,
                    or_(Contact.status == ContactStatus.pending.value,
                        and_(Contact.status == ContactStatus.retry.value, or_(Contact.next_attempt_at.is_(None), Contact.next_attempt_at <= now))),
                ).order_by(Contact.created_at).limit(slots)
            )).all()
            for c in contacts:
                c.status, c.attempts = ContactStatus.calling.value, c.attempts + 1
            from_number = camp.from_number or ""
            await db.commit()
        for c in contacts:
            await self._bucket.take()
            call = await self.calls.create_call(organization_id=org_id, direction="outbound", from_number=from_number,
                                                to_number=c.phone, campaign_id=campaign_id, contact_id=c.id)
            self._active.setdefault(campaign_id, set()).add(call.id)
            self._call_to_contact[call.id] = (campaign_id, c.id)
            asyncio.create_task(self.calls.dial(call))
        return len(contacts)

    async def _is_exhausted(self, campaign_id: uuid.UUID) -> bool:
        async with session_factory()() as db:
            remaining = await db.scalar(select(func.count()).select_from(Contact).where(
                Contact.campaign_id == campaign_id,
                Contact.status.in_([ContactStatus.pending.value, ContactStatus.retry.value, ContactStatus.calling.value])))
            return not remaining

    async def _complete(self, campaign_id: uuid.UUID, org_id: uuid.UUID) -> None:
        async with session_factory()() as db:
            camp = await db.get(Campaign, campaign_id)
            if camp is not None and camp.status == CampaignStatus.running.value:
                camp.status, camp.completed_at = CampaignStatus.completed.value, datetime.now(timezone.utc)
                await db.commit()
        await self._maybe_progress(campaign_id, org_id, force=True)
        await ev.emit(ev.CAMPAIGN_STATUS, org_id, campaign_id=campaign_id, status="completed")

    # ----------------------------------------------------------------- fin d'appel
    async def _on_call_finished(self, call_id: uuid.UUID, status: str) -> None:
        link = self._call_to_contact.pop(call_id, None)
        if link is None:
            return
        campaign_id, contact_id = link
        self._active.get(campaign_id, set()).discard(call_id)
        async with session_factory()() as db:
            contact = await db.get(Contact, contact_id)
            camp = await db.get(Campaign, campaign_id)
            call = await db.get(Call, call_id)
            if contact is None or camp is None:
                return
            outcome = (call.outcome if call else None) or status
            contact.last_outcome = outcome
            if contact.status == ContactStatus.opted_out.value or outcome == "opted_out":
                contact.status = ContactStatus.opted_out.value
            elif (status in RETRYABLE or outcome in RETRY_OUTCOMES) and contact.attempts < camp.max_attempts:
                contact.status = ContactStatus.retry.value
                contact.next_attempt_at = datetime.now(timezone.utc) + timedelta(minutes=camp.retry_delay_minutes)
            elif status == CallStatus.failed.value or outcome in {"failed", "no_answer", "wrong_number"}:
                contact.status = ContactStatus.failed.value
            else:
                contact.status = ContactStatus.completed.value
            org_id = camp.organization_id
            await db.commit()
        await self._maybe_progress(campaign_id, org_id, force=True)

    async def _maybe_progress(self, campaign_id: uuid.UUID, org_id: uuid.UUID, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._progress_last.get(campaign_id, 0) < 1.0:
            return
        self._progress_last[campaign_id] = now
        progress = await campaign_progress(campaign_id, self.active_calls(campaign_id))
        progress.pop("campaign_id", None)
        await ev.emit(ev.CAMPAIGN_PROGRESS, org_id, campaign_id=campaign_id, **progress)


async def campaign_progress(campaign_id: uuid.UUID, active_calls: int = 0) -> dict[str, Any]:
    async with session_factory()() as db:
        camp = await db.get(Campaign, campaign_id)
        rows = (await db.execute(select(Contact.status, func.count()).where(Contact.campaign_id == campaign_id).group_by(Contact.status))).all()
        success = await db.scalar(select(func.count()).select_from(Call).where(Call.campaign_id == campaign_id, Call.outcome == "success"))
    counts = {s.value: 0 for s in ContactStatus}
    counts.update({k: int(v) for k, v in rows})
    total = sum(counts.values())
    done = counts["completed"] + counts["failed"] + counts["opted_out"]
    return {
        "campaign_id": str(campaign_id), "status": camp.status if camp else "unknown", "total_contacts": total,
        "pending": counts["pending"], "calling": counts["calling"], "completed": counts["completed"], "failed": counts["failed"],
        "opted_out": counts["opted_out"], "retry": counts["retry"], "active_calls": active_calls, "success_count": int(success or 0),
        "percent": round(100 * done / total, 1) if total else 0.0,
    }


_dispatcher: CampaignDispatcher | None = None


def get_dispatcher() -> CampaignDispatcher:
    global _dispatcher
    if _dispatcher is None:
        from app.services.call_manager import get_call_manager

        _dispatcher = CampaignDispatcher(get_call_manager())
    return _dispatcher


def set_dispatcher(d: CampaignDispatcher | None) -> None:
    global _dispatcher
    _dispatcher = d
