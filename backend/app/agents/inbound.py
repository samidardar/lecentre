"""Inbound Agent : réceptionniste / standard IA."""
from __future__ import annotations

from typing import Any

from app.agents.base import AgentPlan, register
from app.agents.context import CallContext
from app.agents.prompts import inbound_instructions


class InboundAgent:
    name = "inbound_agent"

    def prefetch_query(self, ctx: CallContext) -> str:
        return f"{ctx.org_name} horaires contact services informations pratiques"

    def plan(self, ctx: CallContext, tools: list[str], prefetched: list[dict[str, Any]]) -> AgentPlan:
        instructions, kickoff, greeting = inbound_instructions(ctx, tools, prefetched)
        return AgentPlan(self.name, instructions, kickoff, greeting, tools, ctx.voice, prefetched)


inbound_agent = register(InboundAgent())
