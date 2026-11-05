"""OpenRouter (API compatible OpenAI) pour la conversation temps réel et l'analyse post-appel.

L'historique interne de la cascade reste au format « blocs » (text / tool_use / tool_result) ; la conversion vers le
format OpenAI (messages system/user/assistant/tool, tool_calls) se fait uniquement ici.
"""
from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

import httpx

from app.core.config import Settings
from app.llm.claude import ToolUse, TurnResult

logger = logging.getLogger(__name__)
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
FINISH_MAP = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens", "content_filter": "refusal"}


class OpenRouterError(RuntimeError):
    pass


def headers(settings: Settings) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "HTTP-Referer": settings.public_base_url,
        "X-Title": "CallWiz AI",
        "Content-Type": "application/json",
    }


def to_openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Outils au format Anthropic ({name, description, input_schema}) → format OpenAI."""
    return [
        {"type": "function", "function": {"name": t["name"], "description": t.get("description", ""), "parameters": t["input_schema"]}}
        for t in tools
    ]


def to_openai_messages(system: str, messages: list[dict[str, Any]], cache: bool = True) -> list[dict[str, Any]]:
    sys_block: dict[str, Any] = {"type": "text", "text": system}
    if cache:  # pris en compte par les modèles Anthropic (cache du préfixe), ignoré ailleurs
        sys_block["cache_control"] = {"type": "ephemeral"}
    out: list[dict[str, Any]] = [{"role": "system", "content": [sys_block]}]
    for m in messages:
        content = m["content"]
        if m["role"] == "user":
            if isinstance(content, str):
                out.append({"role": "user", "content": content})
                continue
            for block in content:
                if block.get("type") == "tool_result":
                    out.append({"role": "tool", "tool_call_id": block["tool_use_id"], "content": block.get("content", "")})
                elif block.get("type") == "text":
                    out.append({"role": "user", "content": block["text"]})
        else:
            if isinstance(content, str):
                out.append({"role": "assistant", "content": content})
                continue
            text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
            calls = [
                {"id": b["id"], "type": "function", "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)}}
                for b in content if b.get("type") == "tool_use"
            ]
            msg: dict[str, Any] = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
    return out


def _parse_args(raw: str) -> dict[str, Any]:
    try:
        val = json.loads(raw or "{}")
        return val if isinstance(val, dict) else {}
    except json.JSONDecodeError:
        logger.warning("arguments d'outil illisibles: %r", raw[:200])
        return {}


class OpenRouterConversation:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        if not settings.openrouter_api_key:
            raise RuntimeError("OPENROUTER_API_KEY manquant (LLM_PROVIDER=openrouter)")
        self.s = settings
        # Connexion persistante (keep-alive) : évite un handshake TLS à chaque tour de parole.
        # keepalive_expiry : par défaut httpx ferme une connexion inactive après 5 s, soit un nouveau handshake TLS
        # (100–300 ms) à presque chaque tour de parole. On garde les connexions 2 minutes.
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0),
                                                   limits=httpx.Limits(max_keepalive_connections=50, max_connections=200,
                                                                       keepalive_expiry=120.0))
        self._last_use = 0.0

    async def warmup(self) -> None:
        """Ouvre (ou garde ouverte) la connexion TLS avant le premier tour de parole d'un appel."""
        import time

        if time.monotonic() - self._last_use < 60:
            return
        self._last_use = time.monotonic()
        try:
            await self._client.get("https://openrouter.ai/api/v1/key", headers=headers(self.s), timeout=5)
        except Exception:
            pass

    def build_body(self, system: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]]) -> dict[str, Any]:
        fallbacks = [m.strip() for m in self.s.openrouter_fallback_models.split(",") if m.strip()]
        body: dict[str, Any] = {
            "model": self.s.openrouter_model,
            "messages": to_openai_messages(system, messages),
            "stream": True,
            "max_tokens": self.s.llm_max_tokens,
            "temperature": self.s.llm_temperature,
            "provider": {"sort": "latency", "allow_fallbacks": True},
        }
        if fallbacks:
            body["models"] = [self.s.openrouter_model, *fallbacks]
        if tools:
            body["tools"] = to_openai_tools(tools)
            body["tool_choice"] = "auto"
        return body

    async def stream(self, system: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]]) -> AsyncIterator[str | TurnResult]:
        import time

        self._last_use = time.monotonic()
        body = self.build_body(system, tools, messages)
        text_parts: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        finish: str | None = None
        usage: dict[str, Any] = {}
        async with self._client.stream("POST", OPENROUTER_URL, headers=headers(self.s), json=body) as resp:
            if resp.status_code >= 400:
                detail = (await resp.aread()).decode(errors="ignore")[:300]
                raise OpenRouterError(f"OpenRouter {resp.status_code}: {detail}")
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue  # lignes vides et commentaires keep-alive (": OPENROUTER PROCESSING")
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                if chunk.get("error"):
                    raise OpenRouterError(str(chunk["error"].get("message", chunk["error"]))[:300])
                if chunk.get("usage"):
                    usage = chunk["usage"]
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        text_parts.append(delta["content"])
                        yield delta["content"]
                    for tc in delta.get("tool_calls") or []:
                        slot = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "arguments": ""})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] = fn["name"]
                        if fn.get("arguments"):
                            slot["arguments"] += fn["arguments"]
                    if choice.get("finish_reason"):
                        finish = choice["finish_reason"]
        tool_uses = [ToolUse(c["id"] or f"call_{i}", c["name"], _parse_args(c["arguments"])) for i, c in sorted(calls.items()) if c["name"]]
        content: list[dict[str, Any]] = []
        text = "".join(text_parts)
        if text:
            content.append({"type": "text", "text": text})
        content += [{"type": "tool_use", "id": t.id, "name": t.name, "input": t.input} for t in tool_uses]
        if not content:
            content = [{"type": "text", "text": ""}]
        stop = "tool_use" if tool_uses else FINISH_MAP.get(finish or "stop", "end_turn")
        yield TurnResult(content, stop, tool_uses, int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0))

    async def aclose(self) -> None:
        await self._client.aclose()


async def complete_json(settings: Settings, prompt: str, schema: dict[str, Any], model: str | None = None) -> dict[str, Any]:
    """Appel non streamé avec sortie JSON structurée (analyse post-appel)."""
    body = {
        "model": model or settings.openrouter_analysis_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 1024,
        "temperature": 0.1,
        "response_format": {"type": "json_schema", "json_schema": {"name": "call_analysis", "strict": True, "schema": schema}},
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(OPENROUTER_URL, headers=headers(settings), json=body)
    if resp.status_code >= 400:
        raise OpenRouterError(f"OpenRouter {resp.status_code}: {resp.text[:300]}")
    text = resp.json()["choices"][0]["message"].get("content") or "{}"
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start : end + 1] if start >= 0 else "{}")
