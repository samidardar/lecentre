"""Serveur MCP de développement pour Claude Code / IDE : inspecter la base, rejouer un appel, tester le RAG.

Ajouter à Claude Code :
  claude mcp add callwiz-dev -- backend/.venv/Scripts/python mcp/devtools/server.py
(exécuter depuis la racine du repo ; utilise la même config .env que le backend)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

try:  # SDK mcp ≥ 2
    from mcp.server.mcpserver import MCPServer as FastMCP  # noqa: E402
except ImportError:  # SDK mcp 1.x
    from mcp.server.fastmcp import FastMCP  # noqa: E402
from sqlalchemy import select, text  # noqa: E402

from app.db import session_factory  # noqa: E402
from app.models import AgentDecision, Base, Call, CallEvent  # noqa: E402

mcp = FastMCP("callwiz-devtools")


@mcp.tool()
def db_schema() -> dict:
    """Tables et colonnes du schéma CallWiz."""
    return {t.name: [f"{c.name}:{c.type}" for c in t.columns] for t in Base.metadata.sorted_tables}


@mcp.tool()
async def sql_readonly(query: str, limit: int = 50) -> list[dict]:
    """Exécute une requête SELECT (lecture seule)."""
    q = query.strip().rstrip(";")
    if not q.lower().startswith(("select", "with")) or any(k in q.lower() for k in (" insert ", " update ", " delete ", " drop ", " alter ")):
        raise ValueError("seules les requêtes SELECT sont autorisées")
    async with session_factory()() as db:
        rows = (await db.execute(text(f"{q} LIMIT {int(limit)}"))).mappings().all()
    return [dict(r) for r in rows]


@mcp.tool()
async def call_timeline(call_id: str) -> dict:
    """Timeline complète d'un appel : statut, transcript, événements, décisions agents, latences, coût."""
    import uuid

    cid = uuid.UUID(call_id)
    async with session_factory()() as db:
        call = await db.get(Call, cid)
        if call is None:
            return {"error": "not found"}
        events = (await db.scalars(select(CallEvent).where(CallEvent.call_id == cid).order_by(CallEvent.created_at))).all()
        decisions = (await db.scalars(select(AgentDecision).where(AgentDecision.call_id == cid).order_by(AgentDecision.created_at))).all()
    return {
        "status": call.status, "outcome": call.outcome, "intent": call.intent, "latency": call.latency, "cost": call.cost_breakdown,
        "transcript": call.transcript, "error": call.error,
        "events": [{"t": e.created_at.isoformat(), "type": e.type, "payload": e.payload} for e in events],
        "decisions": [{"agent": d.agent, "decision": d.decision, "reason": d.reason, "data": d.data} for d in decisions],
    }


@mcp.tool()
async def rag_query(organization_id: str, query: str, top_k: int = 4) -> dict:
    """Interroge le RAG d'une organisation (mêmes paramètres que les agents)."""
    from app.rag.service import get_rag

    res = await get_rag().search(organization_id, query, top_k=top_k)
    return {"low_confidence": res.low_confidence, "latency_ms": res.latency_ms, "hits": [h.as_dict() for h in res.hits]}


if __name__ == "__main__":
    mcp.run()
