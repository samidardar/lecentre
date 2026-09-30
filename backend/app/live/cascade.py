"""Pipeline vocal en cascade : Deepgram (écoute) → Claude (réflexion) → Fish Audio (voix).

Expose la même interface `LiveSession` que Gemini Live, donc CallSession, les agents et la supervision ne changent pas.

Latence :
- le TTS s'ouvre en parallèle de la requête LLM ;
- le texte part vers Fish Audio phrase par phrase (flush) : on parle dès la première phrase ;
- les consignes système et les outils sont mis en cache côté Claude.
Barge-in : dès que Deepgram entend ≥ N mots pendant que l'IA parle, le tour est annulé (`Interrupted`).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from typing import Any, AsyncIterator, Callable

from app.core.config import Settings, get_settings
from app.core.metrics import LIVE_ERRORS
from app.live.base import (
    OUT_RATE, AudioOut, InputTranscript, Interrupted, LiveConfig, LiveEvent, OutputTranscript, SessionEnded, ToolCall,
    ToolCallItem, TurnComplete, Usage,
)
from app.live.deepgram import DeepgramSTT
from app.live.fish import FishTTSStream
from app.llm.claude import ClaudeConversation, ConversationLLM, TurnResult, to_anthropic_tools
from app.voice.audio import Resampler

logger = logging.getLogger(__name__)

_SENTENCE_END = re.compile(r"([.!?…:;])(\s|$)")
OPERATOR_NOTE = (
    "\n\nMESSAGES ENTRE CROCHETS : les messages qui commencent par [APPEL_DEBUT], [SUPERVISEUR], [SILENCE], "
    "[REPONDEUR] ou [DTMF] sont des consignes du système, jamais des paroles de l'interlocuteur. Applique-les sans les "
    "mentionner. Tout le reste est la transcription de ce que dit l'interlocuteur (elle peut contenir des erreurs de "
    "reconnaissance vocale : interprète avec bon sens, fais répéter si c'est incompréhensible)."
)


def split_speakable(buf: str, min_chars: int = 12) -> tuple[str, str]:
    """Coupe le tampon après la dernière fin de phrase (ou une virgule si le tampon s'allonge)."""
    last = None
    for m in _SENTENCE_END.finditer(buf):
        if m.end() >= min_chars:
            last = m.end()
    if last is None and len(buf) > 140:
        idx = buf.rfind(", ")
        last = idx + 2 if idx > 40 else None
    if last is None:
        return "", buf
    return buf[:last].strip(), buf[last:]


class CascadeLiveSession:
    def __init__(self, cfg: LiveConfig, settings: Settings, *, llm: ConversationLLM,
                 stt_factory: Callable[[], Any], tts_factory: Callable[[], Any]) -> None:
        self.cfg = cfg
        self.s = settings
        self.llm = llm
        self._stt_factory = stt_factory
        self._tts_factory = tts_factory
        self._system = cfg.system_instruction + OPERATOR_NOTE
        self._tools = to_anthropic_tools(cfg.tools)
        self.messages: list[dict[str, Any]] = []
        self._q: asyncio.Queue[LiveEvent] = asyncio.Queue()
        self._turn: asyncio.Task[None] | None = None
        self._speaking = False
        self._spoken: list[str] = []
        self._stt: Any = None
        self._stt_task: asyncio.Task[None] | None = None
        self._stt_lock = asyncio.Lock()
        self._user_finals: list[str] = []
        self._pending_notes: list[str] = []
        self._pending_tools: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._closed = False
        self._warm: tuple[Any, asyncio.Task[None]] | None = None
        self._prewarm()

    # ================================================================ TTS préouvert
    def _prewarm(self) -> None:
        """Ouvre la connexion TTS du prochain tour à l'avance (≈ 1 s de handshake économisée)."""
        if self._closed or self._warm is not None:
            return
        tts = self._tts_factory()
        self._warm = (tts, asyncio.create_task(tts.open()))

    def _acquire_tts(self) -> tuple[Any, asyncio.Task[None]]:
        if self._warm is not None:
            tts, opening = self._warm
            self._warm = None
            failed = opening.done() and (opening.cancelled() or opening.exception() is not None)
            stale = opening.done() and not failed and not getattr(tts, "is_open", True)
            if not failed and not stale:
                return tts, opening
            asyncio.create_task(tts.close())
        tts = self._tts_factory()
        return tts, asyncio.create_task(tts.open())

    # ================================================================ entrée audio
    async def send_audio(self, pcm16k: bytes) -> None:
        if self._closed:
            return
        if self._stt is None:
            async with self._stt_lock:
                if self._stt is None:
                    try:
                        stt = self._stt_factory()
                        await stt.start()
                    except Exception as exc:
                        LIVE_ERRORS.labels("stt_connect").inc()
                        await self._q.put(SessionEnded(error=f"STT indisponible: {exc}"))
                        self._closed = True
                        return
                    self._stt = stt
                    self._stt_task = asyncio.create_task(self._read_stt())
        try:
            await self._stt.send(pcm16k)
        except Exception as exc:
            LIVE_ERRORS.labels("stt_send").inc()
            logger.debug("envoi STT échoué: %s", exc)

    async def _read_stt(self) -> None:
        try:
            async for ev in self._stt.events():
                if ev.kind == "partial":
                    if self._speaking and len(ev.text.split()) >= self.s.barge_in_min_words:
                        await self._interrupt()
                elif ev.kind == "final":
                    if self._speaking and len(ev.text.split()) >= self.s.barge_in_min_words:
                        await self._interrupt()
                    self._user_finals.append(ev.text)
                    await self._q.put(InputTranscript(ev.text))
                elif ev.kind == "end_of_turn" and self._user_finals:
                    text = " ".join(self._user_finals)
                    self._user_finals.clear()
                    await self._start_turn(text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._closed:
                LIVE_ERRORS.labels("stt_stream").inc()
                await self._q.put(SessionEnded(error=f"STT interrompu: {exc}"))

    # ================================================================ entrée texte
    async def send_text(self, text: str, *, turn_complete: bool = True, role: str = "user") -> None:
        if self._closed:
            return
        if not turn_complete:
            self._pending_notes.append(text)
            return
        if not text.startswith("["):  # réplique d'un appelant simulé (tests, démo texte)
            await self._q.put(InputTranscript(text))
        await self._start_turn(text)

    async def send_tool_responses(self, responses: list[tuple[str, str, dict[str, Any]]]) -> None:
        for call_id, _name, result in responses:
            fut = self._pending_tools.pop(call_id, None)
            if fut is not None and not fut.done():
                fut.set_result(result)

    # ================================================================ tours de parole
    async def _interrupt(self) -> None:
        if self._turn is not None and not self._turn.done():
            self._turn.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._turn
            await self._q.put(Interrupted())

    async def _start_turn(self, user_text: str) -> None:
        if self._turn is not None and not self._turn.done():
            was_speaking = self._speaking
            self._turn.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._turn
            if was_speaking:
                await self._q.put(Interrupted())
        notes = "\n".join(self._pending_notes)
        self._pending_notes.clear()
        content = f"{notes}\n{user_text}" if notes else user_text
        self.messages.append({"role": "user", "content": content})
        self._turn = asyncio.create_task(self._run_turn())

    async def _run_turn(self) -> None:
        tts, opening = self._acquire_tts()
        audio_task: asyncio.Task[None] | None = None
        tts_used = False
        self._spoken = []
        resample = Resampler(getattr(tts, "sample_rate", OUT_RATE), OUT_RATE)

        async def pump_audio() -> None:
            async for chunk in tts.audio():
                self._speaking = True
                await self._q.put(AudioOut(resample(chunk)))

        async def speak(sentence: str) -> None:
            nonlocal tts_used, audio_task
            if not sentence:
                return
            await opening
            if audio_task is None:
                audio_task = asyncio.create_task(pump_audio())
            tts_used = True
            await tts.send_text(sentence, flush=True)
            self._spoken.append(sentence)
            await self._q.put(OutputTranscript(sentence + " "))

        try:
            for _ in range(6):  # borne les allers-retours outils dans un même tour
                buf = ""
                result: TurnResult | None = None
                async for piece in self.llm.stream(self._system, self._tools, self.messages):
                    if isinstance(piece, TurnResult):
                        result = piece
                        break
                    buf += piece
                    sentence, buf = split_speakable(buf)
                    await speak(sentence)
                await speak(buf.strip())
                if result is None:
                    break
                await self._q.put(Usage(result.input_tokens, result.output_tokens))
                self.messages.append({"role": "assistant", "content": result.content})
                if result.stop_reason != "tool_use" or not result.tool_uses:
                    break
                await self._run_tools(result)
            if tts_used:
                await tts.finish()
                if audio_task is not None:
                    await audio_task
        except asyncio.CancelledError:
            self._record_interruption()
            raise
        except Exception as exc:
            LIVE_ERRORS.labels("turn").inc()
            logger.exception("tour de parole en échec")
            self._repair_history()
            if not tts_used:
                with contextlib.suppress(Exception):
                    await opening
                    await speak("Excusez-moi, je rencontre un petit souci technique. Pouvez-vous répéter ?")
                    await tts.finish()
                    if audio_task is not None:
                        await audio_task
            del exc
        finally:
            if audio_task is not None and not audio_task.done():
                audio_task.cancel()
            opening.cancel()
            await tts.close()
            self._speaking = False
            self._prewarm()
        await self._q.put(TurnComplete())

    async def _run_tools(self, result: TurnResult) -> None:
        loop = asyncio.get_running_loop()
        futures = {t.id: loop.create_future() for t in result.tool_uses}
        self._pending_tools.update(futures)
        await self._q.put(ToolCall([ToolCallItem(t.id, t.name, t.input) for t in result.tool_uses]))
        blocks = []
        for t in result.tool_uses:
            try:
                out = await asyncio.wait_for(futures[t.id], 10)
            except asyncio.TimeoutError:
                out = {"error": "timeout"}
            blocks.append({"type": "tool_result", "tool_use_id": t.id, "content": json.dumps(out, ensure_ascii=False, default=str)})
        self.messages.append({"role": "user", "content": blocks})

    def _pending_tool_ids(self) -> list[str]:
        if not self.messages or self.messages[-1]["role"] != "assistant":
            return []
        content = self.messages[-1]["content"]
        return [b["id"] for b in content if isinstance(b, dict) and b.get("type") == "tool_use"] if isinstance(content, list) else []

    def _repair_history(self) -> None:
        """Un tool_use sans tool_result rend l'historique invalide pour l'API : on le complète."""
        ids = self._pending_tool_ids()
        if ids:
            self.messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": i, "content": json.dumps({"interrupted": True})} for i in ids]})

    def _record_interruption(self) -> None:
        self._repair_history()
        if self.messages and self.messages[-1]["role"] == "user" and self._spoken:
            said = " ".join(self._spoken)
            self.messages.append({"role": "assistant", "content": f"{said} [interrompu par l'interlocuteur]"})
        for fut in self._pending_tools.values():
            if not fut.done():
                fut.cancel()
        self._pending_tools.clear()

    # ================================================================ sortie
    async def events(self) -> AsyncIterator[LiveEvent]:  # type: ignore[override]
        while True:
            ev = await self._q.get()
            yield ev
            if isinstance(ev, SessionEnded):
                return

    async def close(self) -> None:
        self._closed = True
        if self._warm is not None:
            tts, opening = self._warm
            opening.cancel()
            await tts.close()
            self._warm = None
        if self._turn is not None and not self._turn.done():
            self._turn.cancel()
        if self._stt_task is not None:
            self._stt_task.cancel()
        if self._stt is not None:
            await self._stt.close()
        await self._q.put(SessionEnded())


class CascadeLiveModel:
    def __init__(self, settings: Settings | None = None, llm: ConversationLLM | None = None) -> None:
        self.s = settings or get_settings()
        missing = [k for k, v in {"ANTHROPIC_API_KEY": self.s.anthropic_api_key, "DEEPGRAM_API_KEY": self.s.deepgram_api_key,
                                  "FISH_API_KEY": self.s.fish_api_key}.items() if not v]
        if missing and llm is None:
            raise RuntimeError(f"LIVE_PROVIDER=cascade : clés manquantes {', '.join(missing)}")
        self.llm = llm or ClaudeConversation(self.s)

    async def connect(self, config: LiveConfig) -> CascadeLiveSession:
        return CascadeLiveSession(
            config, self.s, llm=self.llm,
            stt_factory=lambda: DeepgramSTT(self.s, language=config.language),
            tts_factory=lambda: FishTTSStream(self.s, config.voice if _looks_like_fish_id(config.voice) else None),
        )


def _looks_like_fish_id(voice: str | None) -> bool:
    return bool(voice) and re.fullmatch(r"[0-9a-f]{32}", voice or "") is not None
