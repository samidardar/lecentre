"""Orchestration LangGraph (plan de contrôle des appels). Les graphes sont compilés une seule fois.

    call_plan : load_config → supervisor_route → prefetch_rag → {inbound_agent | outbound_agent}
    tool      : authorize → {execute | deny} → record
    turn      : supervise → record
    post_call : analyze → cost → persist → notify

Le flux audio (Twilio ⇄ Gemini Live) ne traverse jamais ces graphes : ils sont invoqués au démarrage, sur
appel d'outil et en fin de tour (en tâche de fond), pour ne jamais ajouter de latence à la voix.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from app.agents import tools as toolreg
from app.agents.analytics import analyze_transcript, send_webhook
from app.agents.base import AGENTS, AgentPlan
from app.agents.context import CallContext, CallRuntime, load_call_context
from app.agents.inbound import inbound_agent
from app.agents.outbound import outbound_agent
from app.agents.supervisor import RouteDecision, SupervisorAction, supervisor
from app.core import events as ev
from app.core.metrics import SUPERVISOR_ACTIONS, TOOL_LATENCY
from app.db import session_factory
from app.llm.text import CallAnalysis
from app.models import Call, CallStatus, Contact, ContactStatus
from app.rag.service import get_rag
from app.services.cost import estimate_call_cost

logger = logging.getLogger(__name__)


async def record_decision(ctx: CallContext, agent: str, decision: str, reason: str = "", confidence: float = 1.0, **data: Any) -> None:
    await ev.emit(ev.CALL_AGENT_DECISION, ctx.organization_id, call_id=ctx.call_id, campaign_id=ctx.campaign_id,
                  agent=agent, decision=decision, reason=reason, confidence=confidence, data=data)


# =============================================================== call_plan
class PlanState(TypedDict, total=False):
    call_id: str
    ctx: CallContext
    route: RouteDecision
    prefetched: list[dict[str, Any]]
    plan: AgentPlan


async def _load_config(state: PlanState) -> PlanState:
    if state.get("ctx") is not None:
        return {}
    async with session_factory()() as db:
        return {"ctx": await load_call_context(db, uuid.UUID(state["call_id"]))}


async def _supervisor_route(state: PlanState) -> PlanState:
    ctx = state["ctx"]
    decision = supervisor.route(ctx)
    await record_decision(ctx, "supervisor", "route", decision.reason, route=decision.route, tools=decision.tools, fallback=decision.fallback)
    return {"route": decision}


async def _prefetch_rag(state: PlanState) -> PlanState:
    ctx, route = state["ctx"], state["route"]
    if not route.rag_enabled:
        return {"prefetched": []}
    agent = inbound_agent if route.route == "inbound_agent" else outbound_agent
    try:
        res = await asyncio.wait_for(
            get_rag().search(ctx.organization_id, agent.prefetch_query(ctx), ctx.knowledge_base_ids or None, top_k=3), 0.8
        )
        return {"prefetched": [h.as_dict() for h in res.hits]}
    except Exception as exc:  # le prefetch est un bonus, jamais bloquant
        logger.warning("prefetch RAG ignoré: %s", exc)
        return {"prefetched": []}


def _agent_node(name: str) -> Any:
    async def node(state: PlanState) -> PlanState:
        route = state["route"]
        plan = AGENTS[name].plan(state["ctx"], route.tools, state.get("prefetched", []))
        plan.fallback = route.fallback
        return {"plan": plan}

    return node


def build_call_plan_graph() -> Any:
    g = StateGraph(PlanState)
    g.add_node("load_config", _load_config)
    g.add_node("supervisor_route", _supervisor_route)
    g.add_node("prefetch_rag", _prefetch_rag)
    g.add_node("inbound_agent", _agent_node("inbound_agent"))
    g.add_node("outbound_agent", _agent_node("outbound_agent"))
    g.add_edge(START, "load_config")
    g.add_edge("load_config", "supervisor_route")
    g.add_edge("supervisor_route", "prefetch_rag")
    g.add_conditional_edges("prefetch_rag", lambda s: s["route"].route, {"inbound_agent": "inbound_agent", "outbound_agent": "outbound_agent"})
    g.add_edge("inbound_agent", END)
    g.add_edge("outbound_agent", END)
    return g.compile()


# =============================================================== tool
class ToolState(TypedDict, total=False):
    rt: CallRuntime
    enabled: list[str]
    name: str
    args: dict[str, Any]
    allowed: bool
    reason: str
    result: dict[str, Any]
    duration_ms: float


async def _authorize(state: ToolState) -> ToolState:
    ok, reason = supervisor.authorize_tool(state["rt"], state["name"], state.get("args", {}), state["enabled"])
    return {"allowed": ok, "reason": reason}


async def _execute(state: ToolState) -> ToolState:
    rt, name = state["rt"], state["name"]
    rt.tool_calls += 1
    t0 = time.perf_counter()
    try:
        result = await asyncio.wait_for(toolreg.execute(name, rt, state.get("args", {})), 8)
    except asyncio.TimeoutError:
        result = {"error": "timeout", "instruction": "L'outil ne répond pas : excuse-toi et propose une alternative."}
    except Exception as exc:
        logger.exception("outil %s en erreur", name)
        result = {"error": str(exc)[:200]}
    dt = time.perf_counter() - t0
    TOOL_LATENCY.labels(name).observe(dt)
    return {"result": result, "duration_ms": round(dt * 1000, 1)}


async def _deny(state: ToolState) -> ToolState:
    return {"result": {"error": "refused", "reason": state.get("reason", "")}, "duration_ms": 0.0}


async def _record_tool(state: ToolState) -> ToolState:
    ctx = state["rt"].ctx
    result = state.get("result", {})
    await ev.emit(ev.CALL_TOOL_CALLED, ctx.organization_id, call_id=ctx.call_id, campaign_id=ctx.campaign_id,
                  tool=state["name"], args=state.get("args", {}), allowed=state.get("allowed", False),
                  duration_ms=state.get("duration_ms"), result_preview=str(result)[:300],
                  rag_chunks=[h.get("chunk_id") for h in state["rt"].rag_hits[-3:]] if state["name"] == "search_knowledge_base" else None)
    if not state.get("allowed", False):
        await record_decision(ctx, "supervisor", "tool_denied", state.get("reason", ""), tool=state["name"])
    return {}


def build_tool_graph() -> Any:
    g = StateGraph(ToolState)
    g.add_node("authorize", _authorize)
    g.add_node("execute", _execute)
    g.add_node("deny", _deny)
    g.add_node("record", _record_tool)
    g.add_edge(START, "authorize")
    g.add_conditional_edges("authorize", lambda s: "execute" if s["allowed"] else "deny", {"execute": "execute", "deny": "deny"})
    g.add_edge("execute", "record")
    g.add_edge("deny", "record")
    g.add_edge("record", END)
    return g.compile()


# =============================================================== turn
class TurnState(TypedDict, total=False):
    rt: CallRuntime
    user_text: str
    assistant_text: str
    prefetched: list[dict[str, Any]]
    action: SupervisorAction


async def _supervise(state: TurnState) -> TurnState:
    return {"action": supervisor.review_turn(state["rt"], state.get("user_text", ""), state.get("assistant_text", ""), state.get("prefetched", []))}


async def _record_turn(state: TurnState) -> TurnState:
    a = state["action"]
    SUPERVISOR_ACTIONS.labels(a.action).inc()
    if a.action != "none":
        await record_decision(state["rt"].ctx, "supervisor", a.action, a.reason, a.confidence, **a.data)
    return {}


def build_turn_graph() -> Any:
    g = StateGraph(TurnState)
    g.add_node("supervise", _supervise)
    g.add_node("record", _record_turn)
    g.add_edge(START, "supervise")
    g.add_edge("supervise", "record")
    g.add_edge("record", END)
    return g.compile()


# =============================================================== post_call
class PostCallState(TypedDict, total=False):
    rt: CallRuntime
    final_status: str
    answered: bool
    answered_at: Optional[datetime]
    error: Optional[str]
    latency: dict[str, Any]
    analysis: CallAnalysis
    cost: dict[str, Any]
    duration_s: float


async def _analyze(state: PostCallState) -> PostCallState:
    rt = state["rt"]
    context = {"direction": rt.ctx.direction, "objective": rt.ctx.objective, "outcome": rt.outcome.get("status"),
               "organization": rt.ctx.org_name}
    if rt.voicemail:
        context["outcome"] = "voicemail"
    if state["final_status"] in (CallStatus.no_answer.value, CallStatus.busy.value):
        context["outcome"] = "no_answer"
    analysis = await analyze_transcript(rt.transcript, context)
    if rt.outcome.get("next_best_action"):
        analysis.next_best_action = rt.outcome["next_best_action"]
    return {"analysis": analysis}


async def _cost(state: PostCallState) -> PostCallState:
    rt = state["rt"]
    answered_at = state.get("answered_at")
    duration = (datetime.now(timezone.utc) - answered_at).total_seconds() if answered_at else 0.0
    return {"cost": estimate_call_cost(duration, answered=bool(state.get("answered")), usage=rt.usage), "duration_s": duration}


async def _persist(state: PostCallState) -> PostCallState:
    rt, a, cost = state["rt"], state["analysis"], state["cost"]
    async with session_factory()() as db:
        call = await db.get(Call, uuid.UUID(rt.ctx.call_id))
        if call is None:
            return {}
        call.status = state["final_status"]
        call.ended_at = datetime.now(timezone.utc)
        call.duration_seconds = int(state.get("duration_s", 0))
        call.cost_estimate, call.cost_breakdown = cost["total"], cost
        call.transcript = rt.transcript
        call.outcome, call.intent, call.sentiment = a.outcome, a.intent, a.sentiment
        call.summary, call.next_best_action = a.summary, a.next_best_action
        call.latency = state.get("latency", {})
        call.error = state.get("error")
        if call.contact_id and a.outcome == "opted_out":
            contact = await db.get(Contact, call.contact_id)
            if contact:
                contact.status = ContactStatus.opted_out.value
        await db.commit()
    return {}


async def _notify(state: PostCallState) -> PostCallState:
    rt, a, cost = state["rt"], state["analysis"], state["cost"]
    status = state["final_status"]
    name = {CallStatus.failed.value: ev.CALL_FAILED, CallStatus.transferred.value: ev.CALL_TRANSFERRED}.get(status, ev.CALL_COMPLETED)
    payload = {
        "status": status, "direction": rt.ctx.direction, "outcome": a.outcome, "intent": a.intent, "sentiment": a.sentiment,
        "summary": a.summary, "duration_seconds": int(state.get("duration_s", 0)), "cost_estimate": cost["total"],
        "was_active": bool(state.get("answered")), "objections": a.objections, "error": state.get("error"),
    }
    await ev.emit(name, rt.ctx.organization_id, call_id=rt.ctx.call_id, campaign_id=rt.ctx.campaign_id, **payload)
    if rt.ctx.webhook_url:
        asyncio.create_task(send_webhook(rt.ctx.webhook_url, {
            "event": name, "call_id": rt.ctx.call_id, "campaign_id": rt.ctx.campaign_id, "contact_id": rt.ctx.contact_id,
            **payload, "messages": rt.messages, "appointment": rt.slots.get("appointment"),
        }))
    return {}


def build_post_call_graph() -> Any:
    g = StateGraph(PostCallState)
    for name, fn in [("analyze", _analyze), ("cost", _cost), ("persist", _persist), ("notify", _notify)]:
        g.add_node(name, fn)
    g.add_edge(START, "analyze")
    g.add_edge("analyze", "cost")
    g.add_edge("cost", "persist")
    g.add_edge("persist", "notify")
    g.add_edge("notify", END)
    return g.compile()


class Graphs:
    def __init__(self) -> None:
        self.call_plan = build_call_plan_graph()
        self.tool = build_tool_graph()
        self.turn = build_turn_graph()
        self.post_call = build_post_call_graph()


_graphs: Graphs | None = None


def get_graphs() -> Graphs:
    global _graphs
    if _graphs is None:
        _graphs = Graphs()
    return _graphs
