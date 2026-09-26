"""Abstraction d'un modèle speech-to-speech temps réel (Gemini Live ou mock).

Audio entrant : PCM16 mono 16 kHz little-endian. Audio sortant : PCM16 mono 24 kHz.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol, Union

IN_RATE = 16_000
OUT_RATE = 24_000


@dataclass
class ToolDeclaration:
    name: str
    description: str
    parameters: dict[str, Any]
    non_blocking: bool = False


@dataclass
class LiveConfig:
    system_instruction: str
    voice: str = "Sulafat"
    language: str = "fr"
    tools: list[ToolDeclaration] = field(default_factory=list)
    greeting_hint: str = ""  # utilisé par le mock ; Gemini reçoit la consigne via le kickoff
    resumption_handle: str | None = None
    temperature: float = 0.6


@dataclass
class AudioOut:
    pcm24k: bytes


@dataclass
class InputTranscript:
    text: str


@dataclass
class OutputTranscript:
    text: str


@dataclass
class TurnComplete:
    pass


@dataclass
class Interrupted:
    pass


@dataclass
class ToolCallItem:
    id: str
    name: str
    args: dict[str, Any]


@dataclass
class ToolCall:
    calls: list[ToolCallItem]


@dataclass
class Usage:
    input_tokens: int
    output_tokens: int


@dataclass
class SessionEnded:
    error: str | None = None


LiveEvent = Union[AudioOut, InputTranscript, OutputTranscript, TurnComplete, Interrupted, ToolCall, Usage, SessionEnded]


class LiveSession(Protocol):
    async def send_audio(self, pcm16k: bytes) -> None: ...
    async def send_text(self, text: str, *, turn_complete: bool = True, role: str = "user") -> None: ...
    async def send_tool_responses(self, responses: list[tuple[str, str, dict[str, Any]]]) -> None: ...
    def events(self) -> AsyncIterator[LiveEvent]: ...
    async def close(self) -> None: ...


class LiveModel(Protocol):
    async def connect(self, config: LiveConfig) -> LiveSession: ...
