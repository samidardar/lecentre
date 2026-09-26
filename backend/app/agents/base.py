"""Contrat commun des agents conversationnels (Inbound, Outbound, et futurs agents : vente, support, recouvrement…).

Un agent conversationnel ne parle pas lui-même : il produit un `AgentPlan` (instructions système, kickoff,
greeting, outils) qui configure la session Gemini Live. Ajouter un agent = implémenter `plan()` et l'enregistrer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from app.agents.context import CallContext


@dataclass
class AgentPlan:
    agent: str
    instructions: str
    kickoff: str
    greeting: str
    tools: list[str]
    voice: str
    prefetched: list[dict[str, Any]] = field(default_factory=list)
    fallback: str = "end_politely"


class ConversationalAgent(Protocol):
    name: str

    def plan(self, ctx: CallContext, tools: list[str], prefetched: list[dict[str, Any]]) -> AgentPlan: ...


AGENTS: dict[str, ConversationalAgent] = {}


def register(agent: ConversationalAgent) -> ConversationalAgent:
    AGENTS[agent.name] = agent
    return agent
