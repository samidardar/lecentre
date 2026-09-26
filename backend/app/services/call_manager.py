"""CallManager : cycle de vie des appels (création, numérotation, sessions actives, fin) et capacité globale."""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import func, select

from app.core import events as ev
from app.core.config import get_settings
from app.db import session_factory
from app.models import Call, CallStatus, PhoneNumber
from app.telephony.base import TelephonyProvider
from app.voice.session import CallSession
from app.voice.transport import MediaTransport

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = (CallStatus.queued.value, CallStatus.ringing.value, CallStatus.active.value)
FinishedCallback = Callable[[uuid.UUID, str], Awaitable[None]]


def build_telephony() -> TelephonyProvider:
    s = get_settings()
    if s.telephony_provider == "twilio":
        from app.telephony.twilio_provider import TwilioProvider

        return TwilioProvider(s)
    from app.telephony.mock_provider import MockTelephonyProvider

    return MockTelephonyProvider()


class CallManager:
    def __init__(self, telephony: TelephonyProvider | None = None) -> None:
        self.settings = get_settings()
        self.telephony = telephony or build_telephony()
        self.sessions: dict[uuid.UUID, CallSession] = {}
        self._tasks: dict[uuid.UUID, asyncio.Task[str]] = {}
        self._finished_callbacks: list[FinishedCallback] = []
        self._voicemail_early: set[uuid.UUID] = set()
        if hasattr(self.telephony, "on_answer"):
            self.telephony.on_answer = self._mock_answered  # type: ignore[attr-defined]
            self.telephony.on_unanswered = self.mark_unanswered  # type: ignore[attr-defined]

    def on_finished(self, cb: FinishedCallback) -> None:
        self._finished_callbacks.append(cb)

    # ----------------------------------------------------------------- capacité
    @property
    def local_active(self) -> int:
        return len(self.sessions)

    async def global_active(self) -> int:
        """Compté en base : valable en multi-process (API + worker)."""
        async with session_factory()() as db:
            return int(await db.scalar(select(func.count()).select_from(Call).where(Call.status.in_(ACTIVE_STATUSES))) or 0)

    # ----------------------------------------------------------------- création
    async def create_call(self, *, organization_id: uuid.UUID, direction: str, from_number: str, to_number: str,
                          campaign_id: uuid.UUID | None = None, contact_id: uuid.UUID | None = None,
                          phone_number_id: uuid.UUID | None = None, provider_call_id: str | None = None,
                          status: str = CallStatus.queued.value) -> Call:
        async with session_factory()() as db:
            call = Call(organization_id=organization_id, direction=direction, from_number=from_number, to_number=to_number,
                        campaign_id=campaign_id, contact_id=contact_id, phone_number_id=phone_number_id,
                        provider=self.telephony.name, provider_call_id=provider_call_id, status=status)
            db.add(call)
            await db.commit()
        await ev.emit(ev.CALL_STARTED, organization_id, call_id=call.id, campaign_id=campaign_id, direction=direction,
                      status=status, from_number=from_number, to_number=to_number, contact_id=str(contact_id) if contact_id else None)
        return call

    async def create_inbound(self, to_number: str, from_number: str, provider_call_id: str | None) -> Call | None:
        async with session_factory()() as db:
            pn = await db.scalar(select(PhoneNumber).where(PhoneNumber.number == to_number, PhoneNumber.active.is_(True)))
        if pn is None or pn.direction == "outbound":
            return None
        return await self.create_call(organization_id=pn.organization_id, direction="inbound", from_number=from_number,
                                      to_number=to_number, phone_number_id=pn.id, provider_call_id=provider_call_id,
                                      status=CallStatus.ringing.value)

    async def dial(self, call: Call) -> None:
        """Lance la numérotation d'un appel sortant déjà créé."""
        from_ = call.from_number or self.settings.twilio_default_from or "+33100000000"
        try:
            res = await self.telephony.make_call(str(call.id), call.to_number, from_)
        except Exception as exc:
            logger.warning("échec numérotation %s: %s", call.id, exc)
            await self.mark_unanswered(str(call.id), CallStatus.failed.value, error=str(exc)[:300])
            return
        async with session_factory()() as db:
            row = await db.get(Call, call.id)
            if row is not None and row.status == CallStatus.queued.value:
                row.provider_call_id, row.status = res.provider_call_id, CallStatus.ringing.value
                await db.commit()
        await ev.emit(ev.CALL_RINGING, call.organization_id, call_id=call.id, campaign_id=call.campaign_id, status="ringing")

    # ----------------------------------------------------------------- sessions
    def start_session(self, call_id: uuid.UUID, transport: MediaTransport, *, provider_call_id: str | None = None) -> asyncio.Task[str]:
        if call_id in self._tasks:
            return self._tasks[call_id]

        async def hook(kind: str, action: dict[str, Any]) -> None:
            sid = provider_call_id or await self._provider_id(call_id)
            if not sid:
                return
            try:
                if kind == "transfer":
                    await self.telephony.transfer(sid, action["to"])
                elif kind == "hangup":
                    await self.telephony.hangup(sid)
            except Exception as exc:
                logger.warning("action télécom %s en échec: %s", kind, exc)

        session = CallSession(call_id, transport, telephony_hook=hook)
        if call_id in self._voicemail_early:
            self._voicemail_early.discard(call_id)
            session._voicemail_pending = True
        self.sessions[call_id] = session
        task = asyncio.create_task(self._run(call_id, session), name=f"call-{call_id}")
        self._tasks[call_id] = task
        return task

    async def _run(self, call_id: uuid.UUID, session: CallSession) -> str:
        status = CallStatus.failed.value
        try:
            status = await session.run()
        except Exception:
            logger.exception("session %s interrompue", call_id)
        finally:
            self.sessions.pop(call_id, None)
            self._tasks.pop(call_id, None)
            await self._notify_finished(call_id, status)
        return status

    async def _notify_finished(self, call_id: uuid.UUID, status: str) -> None:
        for cb in self._finished_callbacks:
            try:
                await cb(call_id, status)
            except Exception:
                logger.exception("callback fin d'appel en échec")

    async def _provider_id(self, call_id: uuid.UUID) -> str | None:
        async with session_factory()() as db:
            call = await db.get(Call, call_id)
            return call.provider_call_id if call else None

    async def _mock_answered(self, call_id: str, transport: MediaTransport, voicemail: bool) -> None:
        cid = uuid.UUID(call_id)
        if voicemail:
            self._voicemail_early.add(cid)
        self.start_session(cid, transport)

    async def on_voicemail(self, call_id: uuid.UUID) -> None:
        session = self.sessions.get(call_id)
        if session is not None:
            await session.on_voicemail()
        else:
            self._voicemail_early.add(call_id)

    async def mark_unanswered(self, call_id: str, status: str, error: str | None = None) -> None:
        """Appel non décroché / occupé / échec réseau, avant toute session."""
        cid = uuid.UUID(call_id)
        if cid in self.sessions:
            return
        status = {"no-answer": "no_answer", "canceled": "no_answer"}.get(status, status)
        if status not in {s.value for s in CallStatus}:
            status = CallStatus.failed.value
        async with session_factory()() as db:
            call = await db.get(Call, cid)
            if call is None or call.status not in ACTIVE_STATUSES:
                return
            call.status, call.ended_at, call.error = status, datetime.now(timezone.utc), error
            call.outcome = "no_answer" if status in ("no_answer", "busy") else "failed"
            org_id, campaign_id = call.organization_id, call.campaign_id
            await db.commit()
        name = ev.CALL_FAILED if status == CallStatus.failed.value else ev.CALL_COMPLETED
        await ev.emit(name, org_id, call_id=cid, campaign_id=campaign_id, status=status, outcome="no_answer", was_active=False,
                      direction="outbound", error=error)
        await self._notify_finished(cid, status)

    async def end_call(self, call_id: uuid.UUID) -> bool:
        session = self.sessions.get(call_id)
        if session is None:
            return False
        session.end()
        return True

    async def shutdown(self) -> None:
        for s in list(self.sessions.values()):
            s.end()
        if self._tasks:
            await asyncio.wait(list(self._tasks.values()), timeout=10)
        await self.telephony.close()


_manager: CallManager | None = None


def get_call_manager() -> CallManager:
    global _manager
    if _manager is None:
        _manager = CallManager()
    return _manager


def set_call_manager(m: CallManager | None) -> None:
    global _manager
    _manager = m
