"""Gemini Live API (audio natif) via google-genai.

- VAD / barge-in côté Gemini (automatic_activity_detection) → `Interrupted`.
- Transcriptions entrée/sortie activées (transcript, supervision, analytics).
- Compression de contexte (sliding window) + reprise de session transparente sur `go_away`.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, AsyncIterator

from app.core.config import Settings, get_settings
from app.core.metrics import LIVE_ERRORS
from app.live.base import (
    AudioOut, InputTranscript, Interrupted, LiveConfig, LiveEvent, OutputTranscript, SessionEnded, ToolCall,
    ToolCallItem, TurnComplete, Usage,
)

logger = logging.getLogger(__name__)


def build_connect_config(cfg: LiveConfig, s: Settings) -> Any:
    from google.genai import types

    sens_start = types.StartSensitivity.START_SENSITIVITY_HIGH if s.live_start_sensitivity == "HIGH" else types.StartSensitivity.START_SENSITIVITY_LOW
    sens_end = types.EndSensitivity.END_SENSITIVITY_HIGH if s.live_end_sensitivity == "HIGH" else types.EndSensitivity.END_SENSITIVITY_LOW
    tools = []
    if cfg.tools:
        tools = [types.Tool(function_declarations=[
            types.FunctionDeclaration(
                name=t.name, description=t.description, parameters_json_schema=t.parameters,
                behavior=types.Behavior.NON_BLOCKING if t.non_blocking else None,
            )
            for t in cfg.tools
        ])]
    from app.core.languages import language_profile

    locale = language_profile(cfg.language).locale
    extra: dict[str, Any] = {}
    if s.live_affective_dialog:
        extra["enable_affective_dialog"] = True
    return types.LiveConnectConfig(
        **extra,
        response_modalities=[types.Modality.AUDIO],
        system_instruction=types.Content(parts=[types.Part(text=cfg.system_instruction)]),
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=cfg.voice)),
            language_code=locale if s.live_send_language_code else None,
        ),
        temperature=cfg.temperature,
        tools=tools or None,
        input_audio_transcription=types.AudioTranscriptionConfig(language_codes=[locale] if s.live_send_language_code else None),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        realtime_input_config=types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(
                disabled=False,
                start_of_speech_sensitivity=sens_start,
                end_of_speech_sensitivity=sens_end,
                prefix_padding_ms=s.live_prefix_padding_ms,
                silence_duration_ms=s.live_silence_duration_ms,
            ),
        ),
        context_window_compression=types.ContextWindowCompressionConfig(sliding_window=types.SlidingWindow()),
        session_resumption=types.SessionResumptionConfig(handle=cfg.resumption_handle),
    )


class GeminiLiveSession:
    def __init__(self, client: Any, model: str, cfg: LiveConfig, settings: Settings) -> None:
        self._client = client
        self._model = model
        self._cfg = cfg
        self._settings = settings
        self._stack: contextlib.AsyncExitStack | None = None
        self._session: Any = None
        self._handle: str | None = cfg.resumption_handle
        self._reconnecting = asyncio.Lock()
        self._closed = False

    async def open(self) -> None:
        from google.genai import types  # noqa: F401  (import anticipé : évite la latence au premier tour)

        self._cfg.resumption_handle = self._handle
        stack = contextlib.AsyncExitStack()
        self._session = await stack.enter_async_context(
            self._client.aio.live.connect(model=self._model, config=build_connect_config(self._cfg, self._settings))
        )
        self._stack = stack

    async def _reconnect(self) -> None:
        async with self._reconnecting:
            old = self._stack
            logger.info("Gemini Live: reprise de session (handle=%s)", bool(self._handle))
            await self.open()
            if old:
                with contextlib.suppress(Exception):
                    await old.aclose()

    async def send_audio(self, pcm16k: bytes) -> None:
        from google.genai import types

        if self._closed:
            return
        try:
            await self._session.send_realtime_input(audio=types.Blob(data=pcm16k, mime_type="audio/pcm;rate=16000"))
        except Exception as exc:
            LIVE_ERRORS.labels("send_audio").inc()
            logger.debug("send_audio failed: %s", exc)

    async def send_text(self, text: str, *, turn_complete: bool = True, role: str = "user") -> None:
        from google.genai import types

        if self._closed:
            return
        await self._session.send_client_content(
            turns=[types.Content(role=role, parts=[types.Part(text=text)])], turn_complete=turn_complete
        )

    async def send_tool_responses(self, responses: list[tuple[str, str, dict[str, Any]]]) -> None:
        from google.genai import types

        if self._closed:
            return
        await self._session.send_tool_response(
            function_responses=[types.FunctionResponse(id=i, name=n, response=r) for i, n, r in responses]
        )

    async def events(self) -> AsyncIterator[LiveEvent]:  # type: ignore[override]
        while not self._closed:
            try:
                got_any = False
                # receive() se termine à chaque turn_complete : on relance la boucle.
                async for msg in self._session.receive():
                    got_any = True
                    for ev in self._translate(msg):
                        yield ev
                    if msg.go_away is not None:
                        LIVE_ERRORS.labels("go_away").inc()
                        await self._reconnect()
                        break
                if not got_any and not self._closed:
                    await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._closed:
                    break
                LIVE_ERRORS.labels("receive").inc()
                if self._handle:
                    logger.warning("Gemini Live receive error, tentative de reprise: %s", exc)
                    try:
                        await self._reconnect()
                        continue
                    except Exception as exc2:
                        exc = exc2
                yield SessionEnded(error=str(exc))
                return
        yield SessionEnded()

    def _translate(self, msg: Any) -> list[LiveEvent]:
        out: list[LiveEvent] = []
        if (upd := msg.session_resumption_update) is not None and upd.resumable and upd.new_handle:
            self._handle = upd.new_handle
        sc = msg.server_content
        if sc is not None:
            if sc.interrupted:
                out.append(Interrupted())
            if sc.input_transcription is not None and sc.input_transcription.text:
                out.append(InputTranscript(sc.input_transcription.text))
            if sc.model_turn is not None:
                for part in sc.model_turn.parts or []:
                    if part.inline_data is not None and part.inline_data.data:
                        out.append(AudioOut(part.inline_data.data))
            if sc.output_transcription is not None and sc.output_transcription.text:
                out.append(OutputTranscript(sc.output_transcription.text))
            if sc.turn_complete:
                out.append(TurnComplete())
        if msg.tool_call is not None and msg.tool_call.function_calls:
            out.append(ToolCall([ToolCallItem(fc.id or "", fc.name or "", dict(fc.args or {})) for fc in msg.tool_call.function_calls]))
        if (um := msg.usage_metadata) is not None:
            out.append(Usage(um.prompt_token_count or 0, um.response_token_count or 0))
        return out

    async def close(self) -> None:
        self._closed = True
        if self._stack:
            with contextlib.suppress(Exception):
                await self._stack.aclose()


class GeminiLiveModel:
    def __init__(self, settings: Settings | None = None) -> None:
        from google import genai

        self._settings = settings or get_settings()
        if not self._settings.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY manquant (LIVE_PROVIDER=gemini)")
        http_options = {"api_version": "v1beta"} if self._settings.live_affective_dialog else None
        self._client = genai.Client(api_key=self._settings.gemini_api_key, http_options=http_options)

    async def connect(self, config: LiveConfig) -> GeminiLiveSession:
        session = GeminiLiveSession(self._client, self._settings.gemini_live_model, config, self._settings)
        await session.open()
        return session
