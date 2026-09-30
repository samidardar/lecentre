"""LLM texte (hors hot path) : analyse post-appel, enrichissement RAG. Gemini Flash-Lite ou heuristique."""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Protocol

from pydantic import BaseModel, Field

from app.core.config import get_settings

logger = logging.getLogger(__name__)

INTENTS = ["faq", "appointment", "support", "complaint", "sales", "human_request", "status_request", "contact_info", "other"]
OUTCOMES = ["success", "failed", "callback", "not_interested", "wrong_number", "voicemail", "transferred", "opted_out", "no_answer"]


class CallAnalysis(BaseModel):
    summary: str = ""
    intent: str = "other"
    sentiment: float = Field(0.5, ge=0.0, le=1.0, description="0 = très négatif, 1 = très positif")
    outcome: str = "failed"
    next_best_action: str = ""
    objections: list[str] = Field(default_factory=list)


class TextLLM(Protocol):
    async def analyze_call(self, transcript: list[dict[str, Any]], context: dict[str, Any]) -> CallAnalysis: ...
    async def summarize_chunk(self, text: str) -> str: ...


_POS = re.compile(r"\b(merci|parfait|super|génial|d'accord|oui|très bien|excellent|volontiers|avec plaisir)\b", re.I)
_NEG = re.compile(r"\b(non|pas intéressé|arrêtez|énervé|nul|inadmissible|scandaleux|plainte|problème|jamais|déçu)\b", re.I)
_INTENT_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("human_request", re.compile(r"\b(humain|conseiller|quelqu'un|une personne|un agent)\b", re.I)),
    ("complaint", re.compile(r"\b(plainte|réclamation|inadmissible|scandaleux|pas content|mécontent)\b", re.I)),
    ("appointment", re.compile(r"\b(rendez-vous|rdv|réserver|disponibilit|créneau|annuler)\b", re.I)),
    ("status_request", re.compile(r"\b(statut|suivi|où en est|commande|livraison|dossier)\b", re.I)),
    ("support", re.compile(r"\b(panne|marche pas|bug|erreur|problème technique|aide)\b", re.I)),
    ("sales", re.compile(r"\b(prix|tarif|devis|acheter|offre|abonnement)\b", re.I)),
    ("contact_info", re.compile(r"\b(adresse|téléphone|email|horaires?|ouvert)\b", re.I)),
    ("faq", re.compile(r"\?")),
]


class HeuristicTextLLM:
    """Fallback déterministe (pas de clé API, tests, panne provider)."""

    async def analyze_call(self, transcript: list[dict[str, Any]], context: dict[str, Any]) -> CallAnalysis:
        user_text = " ".join(t["text"] for t in transcript if t.get("role") == "user")
        pos, neg = len(_POS.findall(user_text)), len(_NEG.findall(user_text))
        sentiment = 0.5 if pos + neg == 0 else round(0.15 + 0.7 * pos / (pos + neg), 2)
        intent = next((name for name, rx in _INTENT_RULES if rx.search(user_text)), "other")
        outcome = context.get("outcome") or ("success" if pos > neg and user_text else "failed")
        n_user = sum(1 for t in transcript if t.get("role") == "user")
        summary = f"Appel {context.get('direction', '')} de {n_user} échanges ; intention « {intent} » ; issue « {outcome} »."
        return CallAnalysis(summary=summary, intent=intent, sentiment=sentiment, outcome=outcome,
                            next_best_action="Rappeler" if outcome in {"callback", "voicemail", "no_answer"} else "")

    async def summarize_chunk(self, text: str) -> str:
        return text.split("\n", 1)[0][:200]


class GeminiTextLLM:
    def __init__(self, api_key: str, model: str) -> None:
        from google import genai

        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._fallback = HeuristicTextLLM()

    async def analyze_call(self, transcript: list[dict[str, Any]], context: dict[str, Any]) -> CallAnalysis:
        from google.genai import types

        convo = "\n".join(f"{t['role'].upper()}: {t['text']}" for t in transcript if t.get("text"))[-12000:]
        prompt = (
            "Analyse cet appel d'un centre d'appels IA et renvoie un JSON strict.\n"
            f"Contexte: {json.dumps(context, ensure_ascii=False)}\n"
            f"intent ∈ {INTENTS}\noutcome ∈ {OUTCOMES}\n"
            "sentiment: 0..1 (sentiment de l'interlocuteur). summary: 2 phrases max en français. "
            "objections: objections exprimées par l'interlocuteur (courtes). next_best_action: action suivante recommandée.\n\n"
            f"TRANSCRIPT:\n{convo}"
        )
        try:
            res = await self._client.aio.models.generate_content(
                model=self._model,
                contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=CallAnalysis, temperature=0.1),
            )
            parsed = res.parsed if isinstance(res.parsed, CallAnalysis) else CallAnalysis.model_validate_json(res.text or "{}")
            if parsed.intent not in INTENTS:
                parsed.intent = "other"
            if context.get("outcome"):  # l'outcome explicite de l'agent (set_outcome) prime
                parsed.outcome = context["outcome"]
            return parsed
        except Exception as exc:
            logger.warning("analyse Gemini échouée, fallback heuristique: %s", exc)
            return await self._fallback.analyze_call(transcript, context)

    async def summarize_chunk(self, text: str) -> str:
        try:
            res = await self._client.aio.models.generate_content(
                model=self._model, contents=f"Résume en une phrase factuelle (français):\n{text[:4000]}"
            )
            return (res.text or "").strip()[:500]
        except Exception:
            return await self._fallback.summarize_chunk(text)


class _AnalysisSchema(BaseModel):
    """Schéma sans contraintes numériques (compatible sorties structurées) ; bornes appliquées ensuite."""

    summary: str
    intent: str
    sentiment: float
    outcome: str
    next_best_action: str
    objections: list[str]


class ClaudeTextLLM:
    def __init__(self, api_key: str, model: str, workspace_id: str | None = None) -> None:
        import anthropic

        headers = {"anthropic-workspace-id": workspace_id} if workspace_id else {}
        self._client = anthropic.AsyncAnthropic(api_key=api_key, default_headers=headers, timeout=30.0)
        self._model = model
        self._fallback = HeuristicTextLLM()

    async def analyze_call(self, transcript: list[dict[str, Any]], context: dict[str, Any]) -> CallAnalysis:
        convo = "\n".join(f"{t['role'].upper()}: {t['text']}" for t in transcript if t.get("text"))[-12000:]
        prompt = (
            "Analyse cet appel d'un centre d'appels IA.\n"
            f"Contexte: {json.dumps(context, ensure_ascii=False)}\n"
            f"intent parmi {INTENTS} ; outcome parmi {OUTCOMES} ; sentiment de l'interlocuteur entre 0 et 1 ; "
            "summary en 2 phrases maximum, en français ; objections exprimées par l'interlocuteur (courtes) ; "
            "next_best_action : l'action suivante recommandée.\n\n"
            f"TRANSCRIPT:\n{convo}"
        )
        try:
            res = await self._client.messages.parse(
                model=self._model, max_tokens=1024, output_format=_AnalysisSchema,
                messages=[{"role": "user", "content": prompt}],
            )
            p = res.parsed_output
            if p is None:
                raise ValueError(f"réponse non structurée (stop_reason={res.stop_reason})")
            out = CallAnalysis(
                summary=p.summary, intent=p.intent if p.intent in INTENTS else "other",
                sentiment=min(1.0, max(0.0, p.sentiment)), outcome=p.outcome if p.outcome in OUTCOMES else "failed",
                next_best_action=p.next_best_action, objections=p.objections[:10],
            )
            if context.get("outcome"):  # l'outcome posé par l'agent (set_outcome) prime
                out.outcome = context["outcome"]
            return out
        except Exception as exc:
            logger.warning("analyse Claude échouée, fallback heuristique: %s", exc)
            return await self._fallback.analyze_call(transcript, context)

    async def summarize_chunk(self, text: str) -> str:
        try:
            res = await self._client.messages.create(
                model=self._model, max_tokens=200,
                messages=[{"role": "user", "content": f"Résume en une phrase factuelle (français) :\n{text[:4000]}"}],
            )
            return next((b.text for b in res.content if b.type == "text"), "").strip()[:500]
        except Exception:
            return await self._fallback.summarize_chunk(text)


_llm: TextLLM | None = None


def get_text_llm() -> TextLLM:
    global _llm
    if _llm is None:
        s = get_settings()
        if s.anthropic_api_key and s.live_provider != "mock":
            _llm = ClaudeTextLLM(s.anthropic_api_key, s.analysis_model, s.anthropic_workspace_id)
        elif s.gemini_api_key and s.live_provider == "gemini":
            _llm = GeminiTextLLM(s.gemini_api_key, s.gemini_text_model)
        else:
            _llm = HeuristicTextLLM()
    return _llm


def set_text_llm(llm: TextLLM | None) -> None:
    global _llm
    _llm = llm
