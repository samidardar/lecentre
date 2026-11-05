"""Cache audio des phrases prévisibles (accueil, « Je regarde ça. »…) : lues instantanément au lieu de ~0,8 s de synthèse.

Clé = modèle + voix + vitesse + fréquence + texte normalisé ; stockage disque (DATA_DIR/tts_cache) + mémoire.
Une synthèse concurrente de la même phrase n'est lancée qu'une fois.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
from typing import Any, Callable

from app.core.config import Settings

logger = logging.getLogger(__name__)


class TTSCache:
    def __init__(self, settings: Settings, max_items: int = 512) -> None:
        self.s = settings
        self.dir = Path(settings.data_dir) / "tts_cache"
        self._mem: dict[str, bytes] = {}
        self._inflight: dict[str, asyncio.Task[bytes]] = {}
        self.max_items = max_items

    def key(self, text: str, voice: str | None, speed: float, sample_rate: int) -> str:
        raw = f"{self.s.fish_model}|{voice or ''}|{speed:.2f}|{sample_rate}|{text.strip()}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def get(self, key: str) -> bytes | None:
        if key in self._mem:
            return self._mem[key]
        path = self.dir / f"{key}.pcm"
        if path.exists():
            data = path.read_bytes()
            self._remember(key, data)
            return data
        return None

    def _remember(self, key: str, data: bytes) -> None:
        if len(self._mem) >= self.max_items:
            self._mem.pop(next(iter(self._mem)))
        self._mem[key] = data

    def put(self, key: str, data: bytes) -> None:
        self._remember(key, data)
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            (self.dir / f"{key}.pcm").write_bytes(data)
        except OSError as exc:
            logger.warning("cache audio non écrit: %s", exc)

    async def get_or_synth(self, key: str, text: str, tts_factory: Callable[[], Any]) -> bytes:
        cached = self.get(key)
        if cached is not None:
            return cached
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(self._synth(key, text, tts_factory))
            self._inflight[key] = task
            task.add_done_callback(lambda _: self._inflight.pop(key, None))
        return await task

    async def _synth(self, key: str, text: str, tts_factory: Callable[[], Any]) -> bytes:
        tts = tts_factory()
        try:
            await tts.open()
            pcm = bytearray()

            async def pump() -> None:
                async for chunk in tts.audio():
                    pcm.extend(chunk)

            reader = asyncio.create_task(pump())
            await tts.send_text(text, flush=True)
            await tts.finish()
            await asyncio.wait_for(reader, 30)
        finally:
            await tts.close()
        data = bytes(pcm)
        if data:
            self.put(key, data)
        return data


_cache: TTSCache | None = None


def get_tts_cache(settings: Settings) -> TTSCache:
    global _cache
    if _cache is None or _cache.s is not settings:
        _cache = TTSCache(settings)
    return _cache
