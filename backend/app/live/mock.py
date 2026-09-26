"""Modèle Live simulé : déterministe, sans réseau. Sert aux tests, démos sans clé et au load test 100 appels.

Il reproduit le contrat de Gemini Live : transcriptions, audio PCM 24 kHz, appels d'outils (dont RAG),
fin de tour, interruption (barge-in) quand de l'audio « parlé » arrive pendant qu'il parle.
"""
from __future__ import annotations

import asyncio
import audioop
import random
import re
import uuid
from typing import Any, AsyncIterator

from app.live.base import (
    OUT_RATE, AudioOut, InputTranscript, Interrupted, LiveConfig, LiveEvent, OutputTranscript, SessionEnded,
    ToolCall, ToolCallItem, TurnComplete, Usage,
)

_OPT_OUT = re.compile(r"(ne m'appelez plus|retirez|désinscri|stop|supprimez mes)", re.I)
_HUMAN = re.compile(r"\b(humain|conseiller|une personne|quelqu'un d'autre|un agent)\b", re.I)
_BYE = re.compile(r"\b(au revoir|bonne journée|c'est tout|merci beaucoup, au revoir)\b", re.I)
_NOT_INTERESTED = re.compile(r"(pas intéressé|pas le temps|non merci)", re.I)
_YES = re.compile(r"\b(oui|d'accord|ok|intéressé|volontiers|parfait|ça marche)\b", re.I)
_RDV = re.compile(r"(rendez-vous|rdv|réserver|créneau)", re.I)


class MockLiveSession:
    def __init__(self, cfg: LiveConfig, first_audio_delay: float, realtime_factor: float) -> None:
        self.cfg = cfg
        self._tools = {t.name for t in cfg.tools}
        self._q: asyncio.Queue[LiveEvent] = asyncio.Queue()
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._speaking: asyncio.Task[None] | None = None
        self._closed = False
        self.first_audio_delay = first_audio_delay
        self.realtime_factor = realtime_factor
        self.received_audio_bytes = 0

    # ------------------------------------------------------------------ entrée
    async def send_audio(self, pcm16k: bytes) -> None:
        self.received_audio_bytes += len(pcm16k)
        if self._speaking and not self._speaking.done() and audioop.rms(pcm16k, 2) > 1500:
            self._speaking.cancel()
            await self._q.put(Interrupted())

    async def send_text(self, text: str, *, turn_complete: bool = True, role: str = "user") -> None:
        if not turn_complete:
            return  # contexte injecté silencieusement
        if text.startswith("[APPEL_DEBUT]"):
            self._start(self._say(self.cfg.greeting_hint or "Bonjour, je suis l'assistant virtuel. Comment puis-je vous aider ?"))
        elif text.startswith("[SILENCE]"):
            self._start(self._say("Vous êtes toujours là ?"))
        elif text.startswith("[REPONDEUR]"):
            self._start(self._say("Bonjour, nous vous rappellerons prochainement. Bonne journée."))
        elif text.startswith("[SUPERVISEUR]"):
            self._start(self._say("Pardon, je reformule : je préfère vérifier cette information avant de vous répondre."))
        elif text.startswith("["):
            return
        else:
            await self._q.put(InputTranscript(text))
            self._start(self._respond(text))

    async def send_tool_responses(self, responses: list[tuple[str, str, dict[str, Any]]]) -> None:
        for call_id, _name, resp in responses:
            fut = self._pending.pop(call_id, None)
            if fut and not fut.done():
                fut.set_result(resp)

    def _start(self, coro: Any) -> None:
        if self._speaking and not self._speaking.done():
            self._speaking.cancel()
        self._speaking = asyncio.create_task(coro)

    # ------------------------------------------------------------------ logique
    async def _tool(self, name: str, **args: Any) -> dict[str, Any]:
        if name not in self._tools:
            return {"error": "tool_not_available"}
        cid = uuid.uuid4().hex[:12]
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[cid] = fut
        await self._q.put(ToolCall([ToolCallItem(cid, name, args)]))
        try:
            return await asyncio.wait_for(fut, 10)
        except asyncio.TimeoutError:
            return {"error": "timeout"}

    async def _respond(self, text: str) -> None:
        if _OPT_OUT.search(text):
            await self._tool("set_outcome", status="opted_out", notes="demande de retrait")
            await self._say("C'est noté, vous ne serez plus contacté. Je vous souhaite une bonne journée.")
            await self._tool("end_call", reason="opt_out")
        elif _HUMAN.search(text):
            await self._say("Bien sûr, je vous mets en relation avec un conseiller, ne quittez pas.")
            await self._tool("transfer_to_human", reason="demande explicite")
        elif _BYE.search(text):
            await self._say("Merci pour votre appel, excellente journée !")
            await self._tool("end_call", reason="fin de conversation")
        elif _NOT_INTERESTED.search(text):
            await self._tool("set_outcome", status="not_interested")
            await self._say("Je comprends tout à fait. Merci pour votre temps, bonne journée.")
            await self._tool("end_call", reason="pas intéressé")
        elif _RDV.search(text):
            res = await self._tool("book_appointment", preferred_time="demain 10h", reason=text[:80])
            when = res.get("slot") or "demain à 10 heures"
            await self._tool("set_outcome", status="success", notes=f"rdv {when}")
            await self._say(f"C'est réservé pour {when}. Vous recevrez une confirmation.")
        elif "?" in text:
            res = await self._tool("search_knowledge_base", query=text)
            await self._say(self._answer_from(res))
        elif _YES.search(text):
            await self._tool("set_outcome", status="success", notes="accord de l'interlocuteur")
            await self._say("Très bien, c'est noté. Autre chose ?")
        else:
            await self._say("D'accord, je vous écoute.")

    @staticmethod
    def _answer_from(res: dict[str, Any]) -> str:
        results = res.get("results") or []
        if not results or res.get("low_confidence"):
            return "Je n'ai pas cette information sous la main, je peux vous transférer à un conseiller si vous le souhaitez."
        content: str = results[0]["content"]
        m = re.search(r"(?:^|\n)\s*(?:R|Réponse|A)\s*[:.]\s*(.+)", content, re.S)
        body = m.group(1) if m else re.sub(r"^\[.*?\]\n", "", content)
        body = re.sub(r"^\s*(Q|Question)\s*[:.].*?\n", "", body)
        sentences = re.split(r"(?<=[.!?])\s+", body.strip())
        return " ".join(sentences[:2]).strip()

    async def _say(self, text: str) -> None:
        await asyncio.sleep(self.first_audio_delay * (0.8 + 0.4 * random.random()))
        await self._q.put(OutputTranscript(text))
        duration = max(0.4, len(text.split()) * 0.28)
        chunk_s = 0.1
        n_chunks = int(duration / chunk_s)
        tone = b"\x00\x08" * int(OUT_RATE * chunk_s)  # PCM16 24 kHz, amplitude faible constante
        for _ in range(n_chunks):
            await self._q.put(AudioOut(tone))
            if self.realtime_factor:
                await asyncio.sleep(chunk_s * self.realtime_factor)
        await self._q.put(Usage(input_tokens=len(text) // 3, output_tokens=int(duration * 25)))
        await self._q.put(TurnComplete())

    # ------------------------------------------------------------------ sortie
    async def events(self) -> AsyncIterator[LiveEvent]:  # type: ignore[override]
        while not self._closed:
            ev = await self._q.get()
            yield ev
            if isinstance(ev, SessionEnded):
                return

    async def close(self) -> None:
        self._closed = True
        if self._speaking and not self._speaking.done():
            self._speaking.cancel()
        await self._q.put(SessionEnded())


class MockLiveModel:
    def __init__(self, first_audio_delay: float = 0.25, realtime_factor: float = 0.0) -> None:
        self.first_audio_delay = first_audio_delay
        self.realtime_factor = realtime_factor

    async def connect(self, config: LiveConfig) -> MockLiveSession:
        await asyncio.sleep(0)
        return MockLiveSession(config, self.first_audio_delay, self.realtime_factor)
