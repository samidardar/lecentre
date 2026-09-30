"""Deepgram — transcription en streaming (WebSocket), PCM16 16 kHz.

Événements utiles : transcriptions partielles (barge-in), finales, `speech_final` / `UtteranceEnd` (fin de tour).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import AsyncIterator
from urllib.parse import urlencode

from app.core.config import Settings

logger = logging.getLogger(__name__)


@dataclass
class SttEvent:
    kind: str  # partial | final | end_of_turn | speech_started
    text: str = ""
    speech_final: bool = False


class DeepgramSTT:
    def __init__(self, settings: Settings, language: str = "fr", sample_rate: int = 16000) -> None:
        if not settings.deepgram_api_key:
            raise RuntimeError("DEEPGRAM_API_KEY manquant (LIVE_PROVIDER=cascade)")
        self.s = settings
        params = {
            "model": settings.deepgram_model, "language": language, "encoding": "linear16", "sample_rate": sample_rate,
            "channels": 1, "interim_results": "true", "punctuate": "true", "smart_format": "true", "vad_events": "true",
            "endpointing": settings.deepgram_endpointing_ms, "utterance_end_ms": settings.deepgram_utterance_end_ms,
        }
        self.url = f"wss://api.deepgram.com/v1/listen?{urlencode(params)}"
        self._ws = None

    async def start(self) -> None:
        from websockets.asyncio.client import connect

        self._ws = await connect(self.url, additional_headers={"Authorization": f"Token {self.s.deepgram_api_key}"},
                                 open_timeout=5, ping_interval=10, max_size=2**22)

    async def send(self, pcm16k: bytes) -> None:
        if self._ws is not None:
            await self._ws.send(pcm16k)

    async def events(self) -> AsyncIterator[SttEvent]:
        assert self._ws is not None
        async for raw in self._ws:
            if isinstance(raw, bytes):
                continue
            msg = json.loads(raw)
            kind = msg.get("type")
            if kind == "Results":
                alts = (msg.get("channel") or {}).get("alternatives") or [{}]
                text = (alts[0].get("transcript") or "").strip()
                if msg.get("is_final"):
                    if text:
                        yield SttEvent("final", text, bool(msg.get("speech_final")))
                    if msg.get("speech_final"):
                        yield SttEvent("end_of_turn")
                elif text:
                    yield SttEvent("partial", text)
            elif kind == "UtteranceEnd":
                yield SttEvent("end_of_turn")
            elif kind == "SpeechStarted":
                yield SttEvent("speech_started")

    async def close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.send(json.dumps({"type": "CloseStream"}))
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
