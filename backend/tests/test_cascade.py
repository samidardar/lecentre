"""Pipeline cascade (Deepgram → Claude → Fish Audio) avec des doublures : aucune clé ni réseau."""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

from app.core.config import get_settings
from app.live.base import AudioOut, InputTranscript, Interrupted, LiveConfig, OutputTranscript, ToolCall, ToolDeclaration, TurnComplete
from app.live.cascade import CascadeLiveSession, split_speakable
from app.live.deepgram import SttEvent
from app.llm.claude import TurnResult, ToolUse


class FakeTTS:
    sample_rate = 24000

    def __init__(self) -> None:
        self.texts: list[str] = []
        self._q: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.is_open = True

    async def open(self) -> None:
        await asyncio.sleep(0)

    async def send_text(self, text: str, flush: bool = True) -> None:
        self.texts.append(text)
        for _ in range(3):
            await self._q.put(b"\x00\x01" * 2400)  # 100 ms

    async def finish(self) -> None:
        await self._q.put(None)

    async def audio(self) -> AsyncIterator[bytes]:
        while (chunk := await self._q.get()) is not None:
            await asyncio.sleep(0.01)
            yield chunk

    async def close(self) -> None:
        self.is_open = False


class FakeSTT:
    def __init__(self) -> None:
        self.q: asyncio.Queue[SttEvent] = asyncio.Queue()
        self.sent = 0

    async def start(self) -> None:
        return None

    async def send(self, pcm: bytes) -> None:
        self.sent += len(pcm)

    async def events(self) -> AsyncIterator[SttEvent]:
        while True:
            yield await self.q.get()

    async def close(self) -> None:
        return None


class FakeLLM:
    """Tour 1 : salue. Tour « horaires » : appelle l'outil puis répond avec son résultat. Tour « long » : parle longtemps."""

    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []

    async def stream(self, system: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]]) -> AsyncIterator[Any]:
        self.calls.append([dict(m) for m in messages])
        last = messages[-1]["content"]
        if isinstance(last, list):  # retour d'outil
            yield "Nous sommes ouverts de 9h à 19h. "
            yield "Autre chose ?"
            yield TurnResult([{"type": "text", "text": "Nous sommes ouverts de 9h à 19h. Autre chose ?"}], "end_turn")
        elif "horaires" in last:
            yield TurnResult([{"type": "tool_use", "id": "tu_1", "name": "search_knowledge_base", "input": {"query": "horaires"}}],
                             "tool_use", [ToolUse("tu_1", "search_knowledge_base", {"query": "horaires"})], 100, 10)
        elif "long" in last:
            for i in range(50):
                yield f"Phrase numéro {i} assez longue pour être dite. "
                await asyncio.sleep(0.02)
            yield TurnResult([{"type": "text", "text": "..."}], "end_turn")
        else:
            yield "Bonjour, je suis l'assistante virtuelle. "
            yield "Comment puis-je vous aider ?"
            yield TurnResult([{"type": "text", "text": "Bonjour..."}], "end_turn", [], 50, 12)


def make_session() -> tuple[CascadeLiveSession, FakeLLM, FakeSTT, list[FakeTTS]]:
    llm, stt, ttss = FakeLLM(), FakeSTT(), []

    def tts_factory() -> FakeTTS:
        t = FakeTTS()
        ttss.append(t)
        return t

    cfg = LiveConfig("Tu es un réceptionniste.", tools=[ToolDeclaration("search_knowledge_base", "kb", {"type": "object", "properties": {}})])
    return CascadeLiveSession(cfg, get_settings(), llm=llm, stt_factory=lambda: stt, tts_factory=tts_factory), llm, stt, ttss


async def collect_until(events: AsyncIterator[Any], kind: type, timeout: float = 5.0) -> list[Any]:
    seen = []
    while True:
        ev = await asyncio.wait_for(events.__anext__(), timeout)
        seen.append(ev)
        if isinstance(ev, kind):
            return seen


def test_split_speakable() -> None:
    assert split_speakable("Bonjour, VéloCity Paris. Comment") == ("Bonjour, VéloCity Paris.", "Comment")
    assert split_speakable("Oui.")[0] == ""  # trop court : on attend la suite
    head, rest = split_speakable("mot " * 40 + ", suite")
    assert head.endswith(",") and rest == "suite"


async def test_greeting_then_tool_answer() -> None:
    session, llm, _, ttss = make_session()
    events = session.events()
    await session.send_text("[APPEL_DEBUT] Salue.")
    first = await collect_until(events, TurnComplete)
    assert any(isinstance(e, AudioOut) for e in first)
    assert "".join(e.text for e in first if isinstance(e, OutputTranscript)).startswith("Bonjour")

    await session.send_text("Quels sont vos horaires ?")
    seen = await collect_until(events, ToolCall)
    assert isinstance(seen[0], InputTranscript)
    call = seen[-1].calls[0]
    assert call.name == "search_knowledge_base"
    await session.send_tool_responses([(call.id, call.name, {"results": [{"content": "9h-19h"}]})])
    rest = await collect_until(events, TurnComplete)
    spoken = "".join(e.text for e in rest if isinstance(e, OutputTranscript))
    assert "9h à 19h" in spoken
    # historique valide pour l'API : tool_use suivi de son tool_result
    roles = [m["role"] for m in session.messages]
    assert roles == ["user", "assistant", "user", "assistant", "user", "assistant"]
    assert session.messages[4]["content"][0]["type"] == "tool_result"
    # la connexion TTS du tour suivant est préouverte
    assert session._warm is not None
    await session.close()


async def test_barge_in_interrupts_and_records_partial_answer() -> None:
    session, llm, stt, _ = make_session()
    events = session.events()
    await session.send_text("parle long")
    await collect_until(events, AudioOut)
    await session.send_audio(b"\x00\x00" * 320)  # démarre le STT
    await stt.q.put(SttEvent("partial", "attendez une seconde"))
    seen = await collect_until(events, Interrupted)
    assert not any(isinstance(e, TurnComplete) for e in seen)
    last = session.messages[-1]
    assert last["role"] == "assistant" and "[interrompu" in last["content"]
    # l'utilisateur termine sa phrase → nouveau tour
    await stt.q.put(SttEvent("final", "attendez une seconde, bonjour", True))
    await stt.q.put(SttEvent("end_of_turn"))
    await collect_until(events, TurnComplete)
    assert session.messages[-2]["content"].endswith("attendez une seconde, bonjour")
    await session.close()


async def test_operator_notes_are_merged_into_next_turn() -> None:
    session, llm, _, _ = make_session()
    events = session.events()
    await session.send_text("[SUPERVISEUR] Réponses plus courtes.", turn_complete=False)
    await session.send_text("Bonjour")
    await collect_until(events, TurnComplete)
    assert llm.calls[-1][-1]["content"] == "[SUPERVISEUR] Réponses plus courtes.\nBonjour"
    await session.close()
