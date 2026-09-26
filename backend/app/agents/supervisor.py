"""Supervisor Agent : routage, autorisation des outils, supervision qualité de chaque tour.

Tout est déterministe et < 5 ms : le Supervisor n'est jamais sur le chemin critique de l'audio.
Avec un modèle audio natif, la réponse est déjà prononcée quand on la lit : la supervision agit donc
(1) en amont (instructions, RAG imposé, outils autorisés) et (2) en correction immédiate (steer / transfert / fin).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from app.agents.context import CallContext, CallRuntime
from app.agents.tools import enabled_tool_names

Action = Literal["none", "steer", "transfer", "end"]

_OPT_OUT = re.compile(r"(ne (m'|me )?(appelez|rappelez) plus|retirez[- ]moi|désinscri|supprimez mes données|liste rouge|bloctel)", re.I)
_HUMAN = re.compile(r"\b(un humain|une vraie personne|un conseiller|une personne|parler à quelqu'un|un agent|un opérateur)\b", re.I)
_FRUSTRATION = re.compile(r"\b(n'importe quoi|ridicule|inadmissible|ça suffit|vous comprenez rien|énervé|agacé|marre|nul)\b", re.I)
_DISCLOSURE = re.compile(r"(assistant(e)? virtuel|intelligence artificielle|\bIA\b|agent virtuel|assistant automatique)", re.I)
_CLAIM = re.compile(
    r"(\d+(?:[.,]\d+)?\s*(?:€|euros?|%|pour ?cent|jours?|heures?|h\b|minutes?|semaines?|mois|ans?)|\b\d{1,2}\s*h\s*\d{0,2}\b)",
    re.I,
)
_DIGITS = re.compile(r"\d+")


@dataclass
class RouteDecision:
    route: Literal["inbound_agent", "outbound_agent"]
    tools: list[str]
    rag_enabled: bool
    quality_checks: list[str] = field(default_factory=lambda: ["disclosure", "opt_out", "hallucination", "escalation", "length", "duration"])
    fallback: Literal["transfer_human", "end_politely", "take_message"] = "end_politely"
    reason: str = ""


@dataclass
class SupervisorAction:
    action: Action = "none"
    message: str = ""
    reason: str = ""
    urgent: bool = False
    confidence: float = 1.0
    data: dict[str, Any] = field(default_factory=dict)


class Supervisor:
    max_tool_calls = 25
    max_words_per_turn = 70

    # ----------------------------------------------------------------- routage
    def route(self, ctx: CallContext) -> RouteDecision:
        tools = enabled_tool_names(ctx.allowed_tools)
        if not ctx.transfer_number and "transfer_to_human" in tools:
            tools.remove("transfer_to_human")
        if ctx.direction == "inbound" and "take_message" not in tools:
            tools.append("take_message")
        fallback: Any = "transfer_human" if "transfer_to_human" in tools else ("take_message" if "take_message" in tools else "end_politely")
        route: Any = "outbound_agent" if ctx.direction == "outbound" else "inbound_agent"
        return RouteDecision(
            route=route, tools=tools, rag_enabled="search_knowledge_base" in tools, fallback=fallback,
            reason=f"direction={ctx.direction}, objectif={ctx.objective or 'accueil'}, kb={len(ctx.knowledge_base_ids)}",
        )

    # ----------------------------------------------------------------- outils
    def authorize_tool(self, rt: CallRuntime, name: str, args: dict[str, Any], enabled: list[str]) -> tuple[bool, str]:
        if name not in enabled:
            return False, "outil non autorisé pour cet agent"
        if rt.tool_calls >= self.max_tool_calls:
            return False, "quota d'outils atteint pour cet appel"
        if name == "transfer_to_human" and not rt.ctx.transfer_number:
            return False, "aucun numéro de transfert configuré"
        if name == "book_appointment" and not str(args.get("preferred_time", "")).strip():
            return False, "créneau manquant : demande d'abord la préférence de l'interlocuteur"
        return True, "ok"

    # ----------------------------------------------------------------- tours
    def grounding_corpus(self, rt: CallRuntime, prefetched: list[dict[str, Any]]) -> str:
        c = rt.ctx
        parts = [c.script, c.context, c.org_description, c.objective_description, c.voicemail_message or "",
                 " ".join(str(v) for v in c.contact.values() if v), " ".join(str(v) for v in c.business_hours.values())]
        parts += [h.get("content", "") for h in rt.rag_hits + prefetched]
        parts += [t["text"] for t in rt.transcript if t["role"] == "user"]
        parts += [str(v) for v in rt.slots.values()]
        return " ".join(parts)

    def unsupported_claims(self, text: str, corpus: str) -> list[str]:
        corpus_digits = set(_DIGITS.findall(corpus))
        out = []
        for m in _CLAIM.finditer(text):
            claim = m.group(0)
            if any(d not in corpus_digits for d in _DIGITS.findall(claim)):
                out.append(claim.strip())
        return out

    def review_turn(self, rt: CallRuntime, user_text: str, assistant_text: str, prefetched: list[dict[str, Any]]) -> SupervisorAction:
        ctx = rt.ctx
        can_transfer = bool(ctx.transfer_number) and ctx.is_open_now()

        # 1. Opt-out : prioritaire, conformité.
        if user_text and _OPT_OUT.search(user_text) and rt.outcome.get("status") != "opted_out":
            rt.outcome.update({"status": "opted_out", "notes": "opt-out détecté par le Supervisor"})
            return SupervisorAction("steer", "[SUPERVISEUR] L'interlocuteur demande à ne plus être contacté. Confirme-lui que c'est noté, "
                                    "excuse-toi pour le dérangement, dis au revoir puis appelle end_call.", "opt_out", urgent=True)

        # 2. Demande d'humain non traitée.
        if user_text and _HUMAN.search(user_text) and not rt.pending_action:
            rt.strikes["human"] = rt.strikes.get("human", 0) + 1
            if rt.strikes["human"] >= 2 and can_transfer:
                return SupervisorAction("transfer", "", "demande d'humain répétée", confidence=0.9)
            if rt.strikes["human"] >= 2:
                return SupervisorAction("steer", "[SUPERVISEUR] L'interlocuteur veut parler à un humain mais personne n'est disponible. "
                                        "Excuse-toi et propose de prendre un message (take_message).", "human_unavailable", urgent=True)

        # 3. Annonce IA au premier tour.
        if rt.turn_count == 1 and assistant_text and not _DISCLOSURE.search(assistant_text):
            return SupervisorAction("steer", f"[SUPERVISEUR] Précise dès ta prochaine phrase que tu es l'assistant virtuel de {ctx.org_name}.",
                                    "disclosure_missing")

        # 4. Hallucination : chiffres / prix / délais sans source.
        if assistant_text:
            claims = self.unsupported_claims(assistant_text, self.grounding_corpus(rt, prefetched))
            if claims:
                rt.strikes["hallucination"] = rt.strikes.get("hallucination", 0) + 1
                return SupervisorAction(
                    "steer",
                    f"[SUPERVISEUR] Tu as donné une information non vérifiée ({', '.join(claims)}). Corrige-toi immédiatement : "
                    "dis que tu préfères vérifier cette information, appelle search_knowledge_base, et si rien n'est trouvé propose un message ou un transfert.",
                    "hallucination_suspected", urgent=True, confidence=0.75, data={"claims": claims},
                )

        # 5. Frustration → proposer un humain.
        if user_text and _FRUSTRATION.search(user_text):
            rt.strikes["frustration"] = rt.strikes.get("frustration", 0) + 1
            if rt.strikes["frustration"] >= 2 and can_transfer:
                return SupervisorAction("transfer", "", "frustration persistante", confidence=0.8)
            return SupervisorAction("steer", "[SUPERVISEUR] L'interlocuteur semble agacé. Reste calme, montre de l'empathie en une phrase, "
                                    "va droit au but" + (" et propose un conseiller humain." if can_transfer else "."), "frustration")

        # 6. Boucle : l'assistant répète la même chose.
        last_assistant = [t["text"] for t in rt.transcript if t["role"] == "assistant"][-3:]
        if len(last_assistant) == 3 and len({a.strip().lower()[:60] for a in last_assistant}) == 1:
            return SupervisorAction("transfer" if can_transfer else "end", "", "boucle conversationnelle", confidence=0.7)

        # 7. Réponses trop longues.
        if assistant_text and len(assistant_text.split()) > self.max_words_per_turn:
            rt.strikes["length"] = rt.strikes.get("length", 0) + 1
            return SupervisorAction("steer", "[SUPERVISEUR] Tes réponses sont trop longues pour le téléphone : 1 à 2 phrases maximum.", "too_long")

        # 8. Durée maximale.
        if rt.elapsed > ctx.max_duration_s:
            return SupervisorAction("steer", "[SUPERVISEUR] Durée maximale atteinte : résume, propose un rappel si besoin, dis au revoir et appelle end_call.",
                                    "max_duration", urgent=True)
        return SupervisorAction()


supervisor = Supervisor()
