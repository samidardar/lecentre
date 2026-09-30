"""Claude en streaming pour la conversation téléphonique (texte → TTS phrase par phrase, outils)."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol

from app.core.config import Settings

logger = logging.getLogger(__name__)


@dataclass
class ToolUse:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class TurnResult:
    content: list[dict[str, Any]]  # blocs à réinjecter tels quels dans l'historique
    stop_reason: str | None
    tool_uses: list[ToolUse] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0


class ConversationLLM(Protocol):
    def stream(self, system: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]]) -> AsyncIterator[str | TurnResult]:
        """Produit les fragments de texte au fil de l'eau, puis un `TurnResult` en dernier."""


class ClaudeConversation:
    def __init__(self, settings: Settings) -> None:
        import anthropic

        if not settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY manquant (LIVE_PROVIDER=cascade)")
        self.s = settings
        # Latence avant tout : timeout court, une seule relance.
        self._client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key, timeout=15.0, max_retries=1,
                                                default_headers=anthropic_headers(settings))

    async def stream(self, system: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]]) -> AsyncIterator[str | TurnResult]:
        async with self._client.messages.stream(
            model=self.s.llm_model,
            max_tokens=self.s.llm_max_tokens,
            temperature=self.s.llm_temperature,
            # Consignes + outils identiques pendant tout l'appel → mis en cache (prefix match).
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            tools=tools or [],
            messages=messages,
        ) as stream:
            async for event in stream:
                if event.type == "text":
                    yield event.text
            final = await stream.get_final_message()
        content = [b.model_dump(mode="json", exclude_none=True) for b in final.content]
        tool_uses = [ToolUse(b.id, b.name, b.input if isinstance(b.input, dict) else {}) for b in final.content if b.type == "tool_use"]
        yield TurnResult(content, final.stop_reason, tool_uses, final.usage.input_tokens, final.usage.output_tokens)


def anthropic_headers(settings: Settings) -> dict[str, str]:
    return {"anthropic-workspace-id": settings.anthropic_workspace_id} if settings.anthropic_workspace_id else {}


def to_anthropic_tools(declarations: list[Any]) -> list[dict[str, Any]]:
    return [
        {"name": d.name, "description": d.description, "input_schema": d.parameters, "eager_input_streaming": True}
        for d in declarations
    ]
