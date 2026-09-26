"""Registre des outils disponibles pour les agents (déclarations Gemini + exécution).

Les outils ne dépendent d'aucun provider : RAG via RagService, calendrier/CRM via MCP si configuré, sinon
implémentation native minimale. Chaque outil reçoit le `CallRuntime` et renvoie un dict JSON-sérialisable.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from app.agents.context import CallRuntime
from app.agents.mcp_client import mcp_manager
from app.live.base import ToolDeclaration
from app.rag.service import get_rag

logger = logging.getLogger(__name__)

ToolFn = Callable[[CallRuntime, dict[str, Any]], Awaitable[dict[str, Any]]]
OUTCOME_VALUES = ["success", "callback", "not_interested", "wrong_number", "failed", "opted_out", "transferred", "voicemail"]


async def search_knowledge_base(rt: CallRuntime, args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query", "")).strip()
    if not query:
        return {"results": [], "low_confidence": True}
    res = await get_rag().search(rt.ctx.organization_id, query, rt.ctx.knowledge_base_ids or None, top_k=3)
    hits = [h.as_dict() for h in res.hits]
    rt.rag_hits.extend(hits)
    return {
        "results": [{"content": h["content"][:700], "source": h["source"]} for h in hits],
        "low_confidence": res.low_confidence,
        "instruction": "Information non trouvée : ne pas inventer, proposer message ou transfert." if res.low_confidence else "",
    }


async def book_appointment(rt: CallRuntime, args: dict[str, Any]) -> dict[str, Any]:
    mcp_name = next((n for n in mcp_manager.tools if n.endswith("book_appointment")), None)
    if mcp_name:
        res = await mcp_manager.call(mcp_name, args | {"phone": rt.ctx.caller_number, "organization_id": rt.ctx.organization_id})
        rt.slots["appointment"] = res
        return res
    tz = ZoneInfo(rt.ctx.timezone)
    slot = (datetime.now(tz) + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    preferred = str(args.get("preferred_time") or "")
    label = preferred or slot.strftime("%A %d %B à %Hh%M")
    rt.slots["appointment"] = {"slot": label, "reason": args.get("reason"), "name": args.get("name"), "status": "pending_confirmation"}
    return {"booked": True, "slot": label, "note": "Réservation enregistrée, confirmation envoyée par le client."}


async def take_message(rt: CallRuntime, args: dict[str, Any]) -> dict[str, Any]:
    msg = {"message": args.get("message", ""), "name": args.get("name"), "callback_number": args.get("callback_number") or rt.ctx.caller_number}
    rt.messages.append(msg)
    return {"saved": True}


async def transfer_to_human(rt: CallRuntime, args: dict[str, Any]) -> dict[str, Any]:
    if not rt.ctx.transfer_number or not rt.ctx.is_open_now():
        return {"transferred": False, "reason": "Aucun conseiller disponible. Propose de prendre un message."}
    rt.pending_action = {"type": "transfer", "to": rt.ctx.transfer_number, "reason": args.get("reason", "")}
    rt.outcome.setdefault("status", "transferred")
    return {"transferred": True, "note": "Le transfert aura lieu dès la fin de ta phrase. Ne dis rien de plus."}


async def end_call(rt: CallRuntime, args: dict[str, Any]) -> dict[str, Any]:
    rt.pending_action = {"type": "end", "reason": args.get("reason", "")}
    return {"ok": True, "note": "L'appel va se terminer. Ne dis plus rien."}


async def set_outcome(rt: CallRuntime, args: dict[str, Any]) -> dict[str, Any]:
    status = str(args.get("status", "failed"))
    if status not in OUTCOME_VALUES:
        status = "failed"
    rt.outcome.update({"status": status, "notes": args.get("notes"), "next_best_action": args.get("next_best_action")})
    return {"ok": True}


async def crm_lookup(rt: CallRuntime, args: dict[str, Any]) -> dict[str, Any]:
    mcp_name = next((n for n in mcp_manager.tools if n.endswith("lookup_contact")), None)
    if mcp_name:
        return await mcp_manager.call(mcp_name, {"phone": args.get("phone") or rt.ctx.caller_number})
    return {"contact": rt.ctx.contact or None}


BUILTIN: dict[str, tuple[ToolDeclaration, ToolFn]] = {
    "search_knowledge_base": (
        ToolDeclaration(
            "search_knowledge_base",
            "Recherche dans la base de connaissances de l'entreprise (FAQ, politiques, prix, horaires, procédures). "
            "À appeler avant toute réponse factuelle.",
            {"type": "object", "properties": {"query": {"type": "string", "description": "Question reformulée de façon autonome"}}, "required": ["query"]},
        ),
        search_knowledge_base,
    ),
    "book_appointment": (
        ToolDeclaration(
            "book_appointment", "Réserve un rendez-vous pour l'interlocuteur.",
            {"type": "object", "properties": {
                "preferred_time": {"type": "string", "description": "Date/heure souhaitée, ex. 'mardi 14h'"},
                "reason": {"type": "string"}, "name": {"type": "string"}}, "required": ["preferred_time"]},
        ),
        book_appointment,
    ),
    "take_message": (
        ToolDeclaration(
            "take_message", "Enregistre un message pour l'équipe (nom, numéro de rappel, message).",
            {"type": "object", "properties": {"message": {"type": "string"}, "name": {"type": "string"}, "callback_number": {"type": "string"}}, "required": ["message"]},
        ),
        take_message,
    ),
    "transfer_to_human": (
        ToolDeclaration(
            "transfer_to_human", "Transfère l'appel à un conseiller humain.",
            {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
        ),
        transfer_to_human,
    ),
    "end_call": (
        ToolDeclaration(
            "end_call", "Termine l'appel après avoir dit au revoir.",
            {"type": "object", "properties": {"reason": {"type": "string"}}},
        ),
        end_call,
    ),
    "set_outcome": (
        ToolDeclaration(
            "set_outcome", "Enregistre le résultat de l'appel.",
            {"type": "object", "properties": {
                "status": {"type": "string", "enum": OUTCOME_VALUES}, "notes": {"type": "string"},
                "next_best_action": {"type": "string"}}, "required": ["status"]},
            non_blocking=True,
        ),
        set_outcome,
    ),
    "crm_lookup": (
        ToolDeclaration(
            "crm_lookup", "Récupère la fiche client associée au numéro de l'interlocuteur.",
            {"type": "object", "properties": {"phone": {"type": "string"}}},
        ),
        crm_lookup,
    ),
}

ALWAYS_ON = ("end_call", "set_outcome")


def enabled_tool_names(allowed: list[str]) -> list[str]:
    names = [n for n in allowed if n in BUILTIN or n in mcp_manager.tools]
    for n in ALWAYS_ON:
        if n not in names:
            names.append(n)
    return names


def declarations(names: list[str]) -> list[ToolDeclaration]:
    out: list[ToolDeclaration] = []
    for n in names:
        if n in BUILTIN:
            out.append(BUILTIN[n][0])
        elif (t := mcp_manager.tools.get(n)) is not None:
            out.append(ToolDeclaration(t.qualified_name, t.description[:500], t.input_schema))
    return out


async def execute(name: str, rt: CallRuntime, args: dict[str, Any]) -> dict[str, Any]:
    if name in BUILTIN:
        return await BUILTIN[name][1](rt, args)
    if name in mcp_manager.tools:
        return await mcp_manager.call(name, args)
    return {"error": f"outil inconnu: {name}"}
