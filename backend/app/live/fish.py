"""Fish Audio — synthèse vocale en streaming (WebSocket `wss://api.fish.audio/v1/tts/live`, messages msgpack).

Une connexion par tour de parole : ouverte dès le début du tour (en parallèle de la génération LLM), le texte est
envoyé phrase par phrase avec `flush` pour que le premier son parte au plus vite.
"""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator

import msgpack

from app.core.config import Settings

logger = logging.getLogger(__name__)
FISH_WS_URL = "wss://api.fish.audio/v1/tts/live"


class FishTTSStream:
    def __init__(self, settings: Settings, voice_id: str | None) -> None:
        if not settings.fish_api_key:
            raise RuntimeError("FISH_API_KEY manquant (LIVE_PROVIDER=cascade)")
        self.s = settings
        self.voice_id = voice_id or settings.fish_voice_id
        self._ws: Any = None
        self.sample_rate = settings.fish_sample_rate

    async def open(self) -> None:
        from websockets.asyncio.client import connect

        self._ws = await connect(
            FISH_WS_URL,
            additional_headers={"Authorization": f"Bearer {self.s.fish_api_key}", "model": self.s.fish_model},
            open_timeout=5, ping_interval=20, max_size=2**24,
        )
        request: dict[str, Any] = {
            "text": "", "format": "pcm", "sample_rate": self.sample_rate, "latency": self.s.fish_latency,
            "normalize": True, "prosody": {"speed": self.s.fish_speed, "volume": 0},
        }
        if self.voice_id:
            request["reference_id"] = self.voice_id
        await self._send({"event": "start", "request": request})

    @property
    def is_open(self) -> bool:
        return self._ws is not None and getattr(self._ws.state, "name", "") == "OPEN"

    async def _send(self, msg: dict[str, Any]) -> None:
        await self._ws.send(msgpack.packb(msg, use_bin_type=True))

    async def send_text(self, text: str, flush: bool = True) -> None:
        await self._send({"event": "text", "text": text if text.endswith(" ") else text + " "})
        if flush:
            await self._send({"event": "flush"})

    async def finish(self) -> None:
        await self._send({"event": "stop"})

    async def audio(self) -> AsyncIterator[bytes]:
        """PCM16 mono à `sample_rate`, jusqu'à l'événement `finish`."""
        async for raw in self._ws:
            if not isinstance(raw, (bytes, bytearray)):
                continue
            msg = msgpack.unpackb(raw, raw=False)
            event = msg.get("event")
            if event == "audio" and msg.get("audio"):
                yield bytes(msg["audio"])
            elif event == "finish":
                if msg.get("reason") == "error":
                    logger.warning("Fish Audio: fin sur erreur %s", msg)
                return
            elif event == "log":
                logger.debug("Fish Audio: %s", msg.get("message"))

    async def close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
