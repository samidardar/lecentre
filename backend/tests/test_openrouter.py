"""Adaptateur OpenRouter : conversion des messages, parsing SSE (texte + appels d'outils), sans réseau."""
from __future__ import annotations

import json

import httpx

from app.core.config import Settings
from app.llm.claude import TurnResult
from app.llm.openrouter import OpenRouterConversation, to_openai_messages


def sse(*chunks: dict | str) -> bytes:
    lines = [": OPENROUTER PROCESSING", ""]
    for c in chunks:
        lines += [f"data: {c if isinstance(c, str) else json.dumps(c)}", ""]
    return "\n".join(lines).encode()


def settings() -> Settings:
    return Settings(_env_file=None, openrouter_api_key="test", llm_provider="openrouter")


def test_message_conversion_roundtrip() -> None:
    history = [
        {"role": "user", "content": "[APPEL_DEBUT] Salue."},
        {"role": "assistant", "content": [{"type": "text", "text": "Je regarde."},
                                          {"type": "tool_use", "id": "c1", "name": "search_knowledge_base", "input": {"query": "horaires"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "{\"ok\": true}"}]},
        {"role": "assistant", "content": "Ouvert de 9h à 19h. [interrompu par l'interlocuteur]"},
    ]
    out = to_openai_messages("SYS", history)
    assert out[0]["role"] == "system" and out[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert out[2]["tool_calls"][0]["function"] == {"name": "search_knowledge_base", "arguments": "{\"query\": \"horaires\"}"}
    assert out[3] == {"role": "tool", "tool_call_id": "c1", "content": "{\"ok\": true}"}
    assert out[4]["content"].startswith("Ouvert")


async def test_stream_text_then_tool_call() -> None:
    seen_body: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_body.update(json.loads(request.content))
        assert request.headers["Authorization"] == "Bearer test"
        body = sse(
            {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "Un instant, "}}]},
            {"choices": [{"index": 0, "delta": {"content": "je vérifie."}}]},
            {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_9", "type": "function",
                                                                   "function": {"name": "search_knowledge_base", "arguments": "{\"que"}}]}}]},
            {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "ry\": \"prix\"}"}}]}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 120, "completion_tokens": 18}},
            "[DONE]",
        )
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    llm = OpenRouterConversation(settings(), client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    tools = [{"name": "search_knowledge_base", "description": "kb", "input_schema": {"type": "object", "properties": {}}}]
    pieces = [p async for p in llm.stream("SYS", tools, [{"role": "user", "content": "Combien ?"}])]
    text = "".join(p for p in pieces if isinstance(p, str))
    result = pieces[-1]
    assert isinstance(result, TurnResult)
    assert text == "Un instant, je vérifie."
    assert result.stop_reason == "tool_use" and result.tool_uses[0].input == {"query": "prix"}
    assert result.input_tokens == 120 and result.content[1]["type"] == "tool_use"
    assert seen_body["models"] == ["anthropic/claude-haiku-5.5", "anthropic/claude-haiku-4.5"]
    assert seen_body["tools"][0]["function"]["name"] == "search_knowledge_base"


async def test_http_error_is_raised() -> None:
    llm = OpenRouterConversation(settings(), client=httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(402, json={"error": {"message": "credits"}}))))
    try:
        async for _ in llm.stream("SYS", [], [{"role": "user", "content": "x"}]):
            pass
    except Exception as exc:
        assert "402" in str(exc)
    else:
        raise AssertionError("une erreur HTTP doit remonter")
