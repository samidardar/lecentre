"""Outbound Agent : appels sortants orientés objectif (confirmation RDV, qualification, relance…)."""
from __future__ import annotations

from typing import Any

from app.agents.base import AgentPlan, register
from app.agents.context import CallContext
from app.agents.prompts import OBJECTIVE_LABELS, outbound_instructions


class OutboundAgent:
    name = "outbound_agent"

    def prefetch_query(self, ctx: CallContext) -> str:
        return " ".join(filter(None, [OBJECTIVE_LABELS.get(ctx.objective, ""), ctx.objective_description, "objections fréquentes"]))

    def plan(self, ctx: CallContext, tools: list[str], prefetched: list[dict[str, Any]]) -> AgentPlan:
        instructions, kickoff, greeting = outbound_instructions(ctx, tools, prefetched)
        return AgentPlan(self.name, instructions, kickoff, greeting, tools, ctx.voice, prefetched)


outbound_agent = register(OutboundAgent())
