"""Provider télécom simulé : chaque appel sortant crée un interlocuteur scripté (SimulatedCallerTransport)."""
from __future__ import annotations

import asyncio
import random
import uuid
from typing import Any, Callable

from app.telephony.base import DialResult

DEFAULT_CALLEE_SCRIPTS: list[list[str]] = [
    ["Oui, c'est bien moi.", "Ah oui, ça peut m'intéresser. Quels sont vos horaires ?", "D'accord, parfait. Merci, au revoir."],
    ["Oui c'est moi.", "Non merci, pas intéressé."],
    ["Oui ?", "Je voudrais prendre un rendez-vous.", "Très bien, merci, au revoir."],
    ["C'est moi, oui.", "Je préfère parler à un conseiller."],
]


class MockTelephonyProvider:
    name = "mock"

    def __init__(self, *, answer_rate: float = 1.0, voicemail_rate: float = 0.0, ring_delay: float = 0.05,
                 scripts: list[list[str]] | None = None, think_time: float = 0.05) -> None:
        self.answer_rate = answer_rate
        self.voicemail_rate = voicemail_rate
        self.ring_delay = ring_delay
        self.scripts = scripts or DEFAULT_CALLEE_SCRIPTS
        self.think_time = think_time
        self.on_answer: Callable[[str, Any, bool], Any] | None = None  # branché par CallManager
        self.on_unanswered: Callable[[str, str], Any] | None = None
        self.hangups: list[str] = []
        self.transfers: list[tuple[str, str]] = []

    async def make_call(self, call_id: str, to: str, from_: str, *, detect_voicemail: bool = True) -> DialResult:
        sid = f"MOCK{uuid.uuid4().hex[:24]}"
        asyncio.get_running_loop().call_later(self.ring_delay, lambda: asyncio.ensure_future(self._answer(call_id)))
        return DialResult(sid, "ringing")

    async def _answer(self, call_id: str) -> None:
        from app.voice.transport import SimulatedCallerTransport

        r = random.random()
        if r > self.answer_rate:
            if self.on_unanswered:
                await self.on_unanswered(call_id, random.choice(["no_answer", "busy"]))
            return
        voicemail = random.random() < self.voicemail_rate
        script = [] if voicemail else random.choice(self.scripts)
        transport = SimulatedCallerTransport(script, think_time=self.think_time)
        if self.on_answer:
            await self.on_answer(call_id, transport, voicemail)

    async def hangup(self, provider_call_id: str) -> None:
        self.hangups.append(provider_call_id)

    async def transfer(self, provider_call_id: str, to: str) -> None:
        self.transfers.append((provider_call_id, to))

    async def close(self) -> None:
        return None
