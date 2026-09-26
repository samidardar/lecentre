"""Transports média : ce qui relie une session d'appel au réseau téléphonique.

- `TwilioMediaTransport` : WebSocket Twilio Media Streams (µ-law 8 kHz, bidirectionnel).
- `SimulatedCallerTransport` : appelant simulé (tests, démo, load test) qui « parle » en texte.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Protocol

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from app.live.base import IN_RATE, OUT_RATE
from app.voice.audio import Resampler, pcm16_to_ulaw, ulaw_to_pcm16

logger = logging.getLogger(__name__)


@dataclass
class InboundMedia:
    kind: str  # "audio" | "text" | "dtmf" | "stop" | "start"
    pcm16k: bytes = b""
    text: str = ""
    data: dict[str, Any] | None = None


class MediaTransport(Protocol):
    def receive(self) -> AsyncIterator[InboundMedia]: ...
    async def send_audio(self, pcm24k: bytes) -> None: ...
    async def clear(self) -> None: ...
    async def on_assistant_turn_complete(self) -> None: ...
    async def close(self) -> None: ...


class TwilioMediaTransport:
    FRAME_BYTES_8K = 160  # 20 ms µ-law

    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self.stream_sid: str | None = None
        self.call_sid: str | None = None
        self.custom: dict[str, Any] = {}
        self._up = Resampler(8000, IN_RATE)
        self._down = Resampler(OUT_RATE, 8000)
        self._out_buf = b""
        self._send_lock = asyncio.Lock()
        self._closed = False
        self._mark_n = 0

    async def wait_start(self, timeout: float = 10.0) -> dict[str, Any]:
        """Consomme les messages `connected` puis `start` (qui porte callSid et paramètres personnalisés)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msg = json.loads(await asyncio.wait_for(self.ws.receive_text(), timeout))
            if msg.get("event") == "start":
                start = msg["start"]
                self.stream_sid, self.call_sid = start["streamSid"], start.get("callSid")
                self.custom = start.get("customParameters") or {}
                return start
        raise TimeoutError("Twilio stream start non reçu")

    async def receive(self) -> AsyncIterator[InboundMedia]:  # type: ignore[override]
        buf = b""
        try:
            while True:
                msg = json.loads(await self.ws.receive_text())
                event = msg.get("event")
                if event == "media":
                    if msg["media"].get("track", "inbound") != "inbound":
                        continue
                    buf += ulaw_to_pcm16(base64.b64decode(msg["media"]["payload"]))
                    if len(buf) >= 640:  # 40 ms à 8 kHz PCM16 → envoi groupé à Gemini
                        yield InboundMedia("audio", pcm16k=self._up(buf))
                        buf = b""
                elif event == "dtmf":
                    yield InboundMedia("dtmf", data=msg.get("dtmf"))
                elif event == "stop":
                    yield InboundMedia("stop")
                    return
        except (WebSocketDisconnect, RuntimeError):
            yield InboundMedia("stop")

    async def send_audio(self, pcm24k: bytes) -> None:
        if self._closed or not self.stream_sid:
            return
        ulaw = pcm16_to_ulaw(self._down(pcm24k))
        payload = base64.b64encode(ulaw).decode()
        async with self._send_lock:
            try:
                await self.ws.send_text(json.dumps({"event": "media", "streamSid": self.stream_sid, "media": {"payload": payload}}))
            except Exception:
                self._closed = True

    async def clear(self) -> None:
        if self._closed or not self.stream_sid:
            return
        async with self._send_lock:
            try:
                await self.ws.send_text(json.dumps({"event": "clear", "streamSid": self.stream_sid}))
            except Exception:
                self._closed = True

    async def on_assistant_turn_complete(self) -> None:
        if self._closed or not self.stream_sid:
            return
        self._mark_n += 1
        async with self._send_lock:
            try:
                await self.ws.send_text(json.dumps({"event": "mark", "streamSid": self.stream_sid, "mark": {"name": f"turn-{self._mark_n}"}}))
            except Exception:
                self._closed = True

    async def close(self) -> None:
        self._closed = True
        try:
            await self.ws.close()
        except Exception:
            pass


class SimulatedCallerTransport:
    """Appelant scripté : parle (texte) après chaque fin de tour de l'IA, puis raccroche.

    `answer_delay` simule le temps de décroché ; `think_time` le délai de réponse humain.
    Les répliques passent par `send_text` de la session Live (Gemini sait aussi traiter du texte : utile pour les
    tests de bout en bout avec le vrai modèle, sans téléphone).
    """

    def __init__(self, script: list[str], think_time: float = 0.2, max_wait_s: float = 30.0, voicemail: bool = False) -> None:
        self.script = list(script)
        self.think_time = think_time
        self.max_wait_s = max_wait_s
        self.voicemail = voicemail
        self._turn_done = asyncio.Event()
        self.audio_bytes_received = 0
        self.clears = 0
        self._closed = asyncio.Event()

    async def receive(self) -> AsyncIterator[InboundMedia]:  # type: ignore[override]
        yield InboundMedia("start")
        for line in self.script:
            try:
                await asyncio.wait_for(self._turn_done.wait(), self.max_wait_s)
            except asyncio.TimeoutError:
                break
            if self._closed.is_set():
                return
            self._turn_done.clear()
            await asyncio.sleep(self.think_time)
            yield InboundMedia("text", text=line)
        # après la dernière réplique on laisse l'IA conclure puis on raccroche
        try:
            await asyncio.wait_for(self._closed.wait(), 3.0)
        except asyncio.TimeoutError:
            pass
        yield InboundMedia("stop")

    async def send_audio(self, pcm24k: bytes) -> None:
        self.audio_bytes_received += len(pcm24k)

    async def clear(self) -> None:
        self.clears += 1

    async def on_assistant_turn_complete(self) -> None:
        self._turn_done.set()

    async def close(self) -> None:
        self._closed.set()
        self._turn_done.set()
