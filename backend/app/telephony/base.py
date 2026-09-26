"""Abstraction télécom : aucun agent ne dépend d'un provider précis."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class DialResult:
    provider_call_id: str
    status: str = "ringing"


class TelephonyError(RuntimeError):
    pass


class TelephonyProvider(Protocol):
    name: str

    async def make_call(self, call_id: str, to: str, from_: str, *, detect_voicemail: bool = True) -> DialResult:
        """Lance un appel sortant. Le média arrive ensuite via le transport du provider."""

    async def hangup(self, provider_call_id: str) -> None: ...

    async def transfer(self, provider_call_id: str, to: str) -> None: ...

    async def close(self) -> None: ...
