"""CallSession : une tâche asyncio par appel, isolée (une exception ne sort jamais de la session).

    transport (Twilio / simulé) ──audio 16k──▶ Gemini Live ──audio 24k──▶ transport
                                   ◀── transcriptions / tool calls / interruptions ──
    LangGraph : call_plan (début) · tool (sur tool_call) · turn (fin de tour, en fond) · post_call (fin)
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from app.agents.context import CallContext, CallRuntime
from app.agents.graphs import get_graphs, record_decision
from app.agents.prompts import voicemail_instruction
from app.agents.tools import declarations
from app.core import events as ev
from app.core.config import get_settings
from app.core.logging import call_id_var, org_id_var
from app.core.metrics import ACTIVE_CALLS, CALLS_TOTAL, VOICE_TO_VOICE
from app.db import session_factory
from app.live import get_live_model
from app.live.base import (
    AudioOut, InputTranscript, Interrupted, LiveConfig, LiveSession, OutputTranscript, SessionEnded, ToolCall,
    TurnComplete, Usage,
)
from app.models import Call, CallStatus
from app.voice.audio import EnergyVAD
from app.voice.transport import MediaTransport

logger = logging.getLogger(__name__)

# Actions de fin d'appel (transfert, raccrochage) exécutées par la couche télécom.
TelephonyHook = Callable[[str, dict[str, Any]], Any]


class CallSession:
    def __init__(self, call_id: uuid.UUID, transport: MediaTransport, *, telephony_hook: TelephonyHook | None = None,
                 ctx: CallContext | None = None) -> None:
        self.call_id = call_id
        self.transport = transport
        self.telephony_hook = telephony_hook
        self._ctx = ctx
        self.settings = get_settings()
        self.rt: CallRuntime | None = None
        self.live: LiveSession | None = None
        self.plan: Any = None
        self.final_status = CallStatus.completed.value
        self.error: str | None = None
        self.answered_at: datetime | None = None
        self._done = asyncio.Event()
        self._assistant_buf: list[str] = []
        self._user_buf: list[str] = []
        self._speaking = False
        self._last_user_speech_end: float | None = None
        self._awaiting_first_audio = False
        self._last_activity = time.monotonic()
        self._latencies: list[float] = []
        self._first_audio_at: float | None = None
        self._bg: set[asyncio.Task[Any]] = set()
        self._kicked_off = False
        self._voicemail_pending = False
        self._silence_stage = 0

    # ================================================================= cycle de vie
    async def run(self) -> str:
        call_id_var.set(str(self.call_id))
        tasks: list[asyncio.Task[Any]] = []
        try:
            graphs = get_graphs()
            state = await graphs.call_plan.ainvoke({"call_id": str(self.call_id), "ctx": self._ctx} if self._ctx else {"call_id": str(self.call_id)})
            ctx: CallContext = state["ctx"]
            org_id_var.set(ctx.organization_id)
            self.plan = state["plan"]
            self.rt = CallRuntime(ctx=ctx)
            await self._mark_answered()
            self.live = await get_live_model().connect(LiveConfig(
                system_instruction=self.plan.instructions, voice=self.plan.voice, language=ctx.language,
                tools=declarations(self.plan.tools),
                greeting_hint=self.plan.greeting,
            ))
            await record_decision(ctx, self.plan.agent, "session_started", f"agent={self.plan.agent}", tools=self.plan.tools)
            if ctx.direction == "inbound" or self._voicemail_pending:
                await self._kickoff()
            tasks = [
                asyncio.create_task(self._pump_in(), name="pump_in"),
                asyncio.create_task(self._pump_out(), name="pump_out"),
                asyncio.create_task(self._watchdog(), name="watchdog"),
            ]
            if ctx.direction == "outbound":
                tasks.append(asyncio.create_task(self._outbound_kickoff_timer()))
            await self._done.wait()
        except Exception as exc:
            logger.exception("session d'appel en échec")
            self.final_status, self.error = CallStatus.failed.value, str(exc)[:500]
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._finalize()
        return self.final_status

    def end(self, status: str | None = None) -> None:
        if status:
            self.final_status = status
        self._done.set()

    async def _mark_answered(self) -> None:
        assert self.rt is not None
        self.answered_at = datetime.now(timezone.utc)
        async with session_factory()() as db:
            call = await db.get(Call, self.call_id)
            if call is not None:
                call.status = CallStatus.active.value
                call.answered_at = self.answered_at
                await db.commit()
        ACTIVE_CALLS.labels(self.rt.ctx.direction).inc()
        await ev.emit(ev.CALL_ANSWERED, self.rt.ctx.organization_id, call_id=self.call_id, campaign_id=self.rt.ctx.campaign_id,
                      direction=self.rt.ctx.direction, agent=self.plan.agent, status=CallStatus.active.value)

    async def _kickoff(self) -> None:
        if self._kicked_off or self.live is None or self.rt is None:
            return
        self._kicked_off = True
        text = voicemail_instruction(self.rt.ctx) if self._voicemail_pending else self.plan.kickoff
        self._awaiting_first_audio, self._last_user_speech_end = True, time.monotonic()
        await self.live.send_text(text, turn_complete=True)

    async def _outbound_kickoff_timer(self) -> None:
        # En sortant on laisse l'interlocuteur dire « Allô » ; sinon on parle au bout de 1,2 s.
        await asyncio.sleep(1.2)
        await self._kickoff()

    async def on_voicemail(self) -> None:
        """Appelé par la détection de répondeur (AMD Twilio)."""
        if self.rt is None:
            self._voicemail_pending = True
            return
        self.rt.voicemail = True
        self.rt.outcome.setdefault("status", "voicemail")
        await record_decision(self.rt.ctx, "outbound_agent", "voicemail_detected", self.rt.ctx.voicemail_behavior)
        if self.rt.ctx.voicemail_behavior == "hangup":
            self.end(CallStatus.voicemail.value)
            return
        self.final_status = CallStatus.voicemail.value
        if self.live is not None:
            await self.transport.clear()
            self._kicked_off = True
            await self.live.send_text(voicemail_instruction(self.rt.ctx), turn_complete=True)

    # ================================================================= flux entrant
    async def _pump_in(self) -> None:
        assert self.live is not None and self.rt is not None
        vad = EnergyVAD()
        async for media in self.transport.receive():
            if media.kind == "audio":
                started, ended = vad.update(media.pcm16k)
                if started:
                    self._last_activity = time.monotonic()
                    self._silence_stage = 0
                    if not self._kicked_off and self.rt.ctx.direction == "outbound":
                        # l'interlocuteur a dit « Allô » : on laisse finir puis on parle
                        asyncio.get_running_loop().call_later(0.5, lambda: self._spawn(self._kickoff()))
                if ended:
                    self._last_user_speech_end = time.monotonic()
                    self._awaiting_first_audio = True
                await self.live.send_audio(media.pcm16k)
            elif media.kind == "text":
                self._last_activity = time.monotonic()
                self._silence_stage = 0
                self._last_user_speech_end, self._awaiting_first_audio = time.monotonic(), True
                await self.live.send_text(media.text, turn_complete=True)
            elif media.kind == "dtmf":
                digit = (media.data or {}).get("digit", "")
                await self.live.send_text(f"[DTMF] L'interlocuteur a tapé la touche {digit} sur son clavier.", turn_complete=True)
            elif media.kind == "stop":
                self.end()
                return

    # ================================================================= flux sortant
    async def _pump_out(self) -> None:
        assert self.live is not None and self.rt is not None
        ctx = self.rt.ctx
        async for event in self.live.events():
            if isinstance(event, AudioOut):
                if self._awaiting_first_audio and self._last_user_speech_end is not None:
                    latency = time.monotonic() - self._last_user_speech_end
                    self._awaiting_first_audio = False
                    self._latencies.append(latency)
                    VOICE_TO_VOICE.observe(latency)
                    self._spawn(ev.emit(ev.CALL_LATENCY, ctx.organization_id, call_id=self.call_id, voice_to_voice_ms=round(latency * 1000, 1)))
                if not self._speaking:
                    self._speaking = True
                    self._spawn(ev.emit(ev.CALL_TTS_STARTED, ctx.organization_id, call_id=self.call_id))
                self._last_activity = time.monotonic()
                await self.transport.send_audio(event.pcm24k)
            elif isinstance(event, InputTranscript):
                self._user_buf.append(event.text)
                self._last_activity = time.monotonic()
                self._spawn(ev.emit(ev.CALL_TRANSCRIPT_PARTIAL, ctx.organization_id, call_id=self.call_id, role="user", text=event.text))
            elif isinstance(event, OutputTranscript):
                self._assistant_buf.append(event.text)
            elif isinstance(event, Interrupted):
                self._speaking = False
                await self.transport.clear()
                self._flush_assistant(interrupted=True)
                self._spawn(ev.emit(ev.CALL_USER_INTERRUPTED, ctx.organization_id, call_id=self.call_id))
            elif isinstance(event, ToolCall):
                await self._handle_tools(event)
            elif isinstance(event, TurnComplete):
                await self._on_turn_complete()
            elif isinstance(event, Usage):
                self.rt.usage["input_tokens"] += event.input_tokens
                self.rt.usage["output_tokens"] += event.output_tokens
            elif isinstance(event, SessionEnded):
                if event.error:
                    self.final_status, self.error = CallStatus.failed.value, event.error
                self.end()
                return

    def _flush_user(self) -> str:
        text = " ".join(t.strip() for t in self._user_buf if t.strip()).strip()
        self._user_buf.clear()
        if text and self.rt:
            self.rt.add_turn("user", text)
            self._spawn(ev.emit(ev.CALL_TRANSCRIPT_FINAL, self.rt.ctx.organization_id, call_id=self.call_id, role="user", text=text))
        return text

    def _flush_assistant(self, interrupted: bool = False) -> str:
        text = "".join(self._assistant_buf).strip()
        self._assistant_buf.clear()
        if text and self.rt:
            self.rt.add_turn("assistant", text, interrupted=interrupted)
            self._spawn(ev.emit(ev.CALL_RESPONSE_GENERATED, self.rt.ctx.organization_id, call_id=self.call_id,
                                role="assistant", text=text, interrupted=interrupted))
        return text

    async def _on_turn_complete(self) -> None:
        assert self.rt is not None
        self._speaking = False
        user_text = self._flush_user()
        assistant_text = self._flush_assistant()
        self.rt.turn_count += 1
        self._last_activity = time.monotonic()
        await self.transport.on_assistant_turn_complete()
        if self.rt.pending_action:
            self._spawn(self._execute_pending_action())
            return
        self._spawn(self._supervise(user_text, assistant_text))

    async def _supervise(self, user_text: str, assistant_text: str) -> None:
        assert self.rt is not None and self.live is not None
        state = await get_graphs().turn.ainvoke({"rt": self.rt, "user_text": user_text, "assistant_text": assistant_text,
                                                 "prefetched": self.plan.prefetched})
        action = state["action"]
        if action.action == "steer":
            await self.live.send_text(action.message, turn_complete=action.urgent)
            if action.urgent:
                self._awaiting_first_audio, self._last_user_speech_end = False, None
        elif action.action in ("transfer", "end"):
            if action.action == "transfer" and self.rt.ctx.transfer_number:
                self.rt.pending_action = {"type": "transfer", "to": self.rt.ctx.transfer_number, "reason": action.reason}
                await self.live.send_text("[SUPERVISEUR] Annonce en une phrase que tu transfères l'appel à un conseiller.", turn_complete=True)
            else:
                self.rt.pending_action = {"type": "end", "reason": action.reason}
                await self.live.send_text("[SUPERVISEUR] Conclus poliment l'appel en une phrase et dis au revoir.", turn_complete=True)

    async def _handle_tools(self, event: ToolCall) -> None:
        assert self.rt is not None and self.live is not None
        graphs = get_graphs()
        results = await asyncio.gather(*(
            graphs.tool.ainvoke({"rt": self.rt, "enabled": self.plan.tools, "name": c.name, "args": c.args}) for c in event.calls
        ))
        await self.live.send_tool_responses([(c.id, c.name, r.get("result", {})) for c, r in zip(event.calls, results)])
        # end_call/transfer : si l'IA ne parle pas (ou plus), on exécute immédiatement, sinon à la fin du tour.
        if self.rt.pending_action and not self._speaking:
            self._spawn(self._execute_pending_action(delay=0.8))

    async def _execute_pending_action(self, delay: float = 0.3) -> None:
        assert self.rt is not None
        action = self.rt.pending_action
        if not action:
            return
        await asyncio.sleep(delay)  # laisse la fin de l'audio partir vers le réseau
        if self._speaking:
            return  # un TurnComplete relancera l'action
        self.rt.pending_action = None
        if action["type"] == "transfer":
            await record_decision(self.rt.ctx, "supervisor", "transfer_executed", action.get("reason", ""), to=action.get("to"))
            await ev.emit(ev.CALL_UPDATED, self.rt.ctx.organization_id, call_id=self.call_id, status="transferring")
            if self.telephony_hook:
                await self.telephony_hook("transfer", action)
            self.end(CallStatus.transferred.value)
        else:
            if self.telephony_hook:
                await self.telephony_hook("hangup", action)
            self.end(self.final_status if self.final_status != CallStatus.failed.value else CallStatus.completed.value)

    # ================================================================= silences & durée
    async def _watchdog(self) -> None:
        assert self.rt is not None and self.live is not None
        s = self.settings
        while True:
            await asyncio.sleep(0.5)
            if self.rt.elapsed > self.rt.ctx.max_duration_s + 30:
                await record_decision(self.rt.ctx, "supervisor", "hard_timeout", "durée max dépassée")
                self.end()
                return
            if self._speaking or not self._kicked_off or self.rt.pending_action:
                continue
            idle = time.monotonic() - self._last_activity
            if idle > s.silence_hangup_s and self._silence_stage < 3:
                self._silence_stage = 3
                self.rt.pending_action = {"type": "end", "reason": "silence prolongé"}
                await self.live.send_text("[SILENCE] L'interlocuteur ne répond plus. Dis poliment que tu vas raccrocher et au revoir.", turn_complete=True)
            elif idle > s.silence_close_s and self._silence_stage < 2:
                self._silence_stage = 2
                self._last_activity = time.monotonic() - s.silence_close_s + 1
                await self.live.send_text("[SILENCE] Toujours pas de réponse. Propose de rappeler plus tard, en une phrase.", turn_complete=True)
            elif idle > s.silence_nudge_s and self._silence_stage < 1:
                self._silence_stage = 1
                await self.live.send_text("[SILENCE] L'interlocuteur est silencieux. Relance-le très brièvement (ex : « Vous êtes toujours là ? »).", turn_complete=True)

    # ================================================================= fin
    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def _finalize(self) -> None:
        with contextlib.suppress(Exception):
            if self.live is not None:
                await self.live.close()
        with contextlib.suppress(Exception):
            await self.transport.close()
        if self._bg:
            await asyncio.wait(list(self._bg), timeout=2)
        if self.rt is None:
            await self._fail_before_start()
            return
        self._flush_user()
        self._flush_assistant()
        ACTIVE_CALLS.labels(self.rt.ctx.direction).dec()
        CALLS_TOTAL.labels(self.rt.ctx.direction, self.final_status).inc()
        lat = sorted(self._latencies)
        latency = {
            "voice_to_voice_ms": [round(x * 1000, 1) for x in lat[:200]],
            "avg_ms": round(sum(lat) / len(lat) * 1000, 1) if lat else None,
            "p95_ms": round(lat[min(len(lat) - 1, int(len(lat) * 0.95))] * 1000, 1) if lat else None,
        }
        try:
            await get_graphs().post_call.ainvoke({
                "rt": self.rt, "final_status": self.final_status, "answered": self.answered_at is not None,
                "answered_at": self.answered_at, "error": self.error, "latency": latency,
            })
        except Exception:
            logger.exception("post_call en échec")

    async def _fail_before_start(self) -> None:
        org_id = None
        async with session_factory()() as db:
            call = await db.get(Call, self.call_id)
            if call is not None:
                call.status, call.error, call.ended_at = CallStatus.failed.value, self.error, datetime.now(timezone.utc)
                org_id = call.organization_id
                await db.commit()
        if org_id:
            await ev.emit(ev.CALL_FAILED, org_id, call_id=self.call_id, status="failed", error=self.error, was_active=False)
