"""Instructions système. Optimisées pour la voix : court, naturel, une question à la fois, zéro invention."""
from __future__ import annotations

import string
from typing import Any

from app.agents.context import CallContext
from app.core.config import get_settings
from app.core.languages import language_profile

OBJECTIVE_LABELS = {
    "confirm_appointment": "confirmer le rendez-vous de l'interlocuteur",
    "qualify_lead": "qualifier l'intérêt et le besoin de l'interlocuteur",
    "request_payment": "obtenir un engagement de règlement pour le montant dû",
    "collect_feedback": "recueillir l'avis de l'interlocuteur (note sur 10 + commentaire)",
    "invite_event": "inviter l'interlocuteur à l'événement et obtenir sa réponse",
    "reactivate_customer": "comprendre pourquoi le client est inactif et lui proposer de revenir",
    "schedule_meeting": "fixer un rendez-vous avec l'interlocuteur",
    "custom": "atteindre l'objectif décrit ci-dessous",
}

VOICE_RULES = """RÈGLES DE CONVERSATION VOCALE (impératives) :
- Tu es au téléphone. Réponds en 1 à 3 phrases courtes. Jamais de listes, de markdown, d'émojis ni d'URL.
- Une seule question à la fois. Laisse parler l'interlocuteur ; s'il te coupe, arrête-toi et écoute.
- Utilise des accusés de réception brefs et naturels (« D'accord », « Très bien », « Je vois »), sans en abuser.
- Confirme les informations importantes (date, heure, nom, numéro) en les reformulant.
- Dis les nombres, dates et prix de façon naturelle à l'oral.
- Si tu n'as pas compris, demande poliment de répéter ou reformule.
- Ton : {tone}.

LANGUE ({language_name}) :
{language_rules}

TRANSPARENCE ET CONFORMITÉ :
- Dès ta première phrase, précise que tu es un assistant virtuel (IA) de {org}.
- Si l'interlocuteur demande à ne plus être contacté : acquiesce, appelle set_outcome(status="opted_out") puis end_call.
- Ne demande que les données strictement nécessaires. Ne lis jamais à voix haute de données sensibles.

FIABILITÉ (anti-hallucination) :
- Avant de donner toute information factuelle sur {org} (prix, délais, horaires, politiques, procédures, produits),
  appelle search_knowledge_base. N'utilise que ce qui est dans les résultats, les connaissances clés ou le script.
- Si l'information est absente ou incertaine : ne l'invente pas. Dis que tu vas faire vérifier, propose de prendre
  un message (take_message) ou de transférer à un humain (transfer_to_human) si disponible.
- N'invente jamais de prix, délai, promesse commerciale ou information légale.

OUTILS :
- Appelle les outils sans l'annoncer longuement ; pendant une recherche tu peux dire « Je regarde ça. ».
- Pour terminer l'appel : dis au revoir brièvement puis appelle end_call.
- Si l'interlocuteur demande un humain{transfer_hint}.
"""


def _fmt(template: str, values: dict[str, Any]) -> str:
    """Substitution tolérante des variables {first_name} etc. (variable absente → chaîne vide)."""

    class _Safe(dict):  # type: ignore[type-arg]
        def __missing__(self, key: str) -> str:
            return ""

    try:
        return string.Formatter().vformat(template, (), _Safe({k: v if v is not None else "" for k, v in values.items()}))
    except (ValueError, IndexError):
        return template


def knowledge_block(hits: list[dict[str, Any]]) -> str:
    if not hits:
        return ""
    lines = [f"[{i + 1}] {h['content'][:600]}" for i, h in enumerate(hits)]
    return "CONNAISSANCES CLÉS (extraits de la base du client, à utiliser en priorité) :\n" + "\n".join(lines)


def common_rules(ctx: CallContext, tools: list[str]) -> str:
    transfer_hint = (
        " : propose le transfert puis appelle transfer_to_human" if "transfer_to_human" in tools
        else " : explique qu'aucun conseiller n'est disponible et propose de prendre un message"
    )
    lang = language_profile(ctx.language)
    lock = (
        f"- Même si l'interlocuteur parle une autre langue, réponds en {lang.name} ; s'il ne comprend pas, "
        "propose poliment un transfert ou un message."
        if get_settings().live_language_lock else
        f"- Commence en {lang.name} ; si l'interlocuteur parle clairement une autre langue, continue dans sa langue."
    )
    return VOICE_RULES.format(tone=ctx.tone, language_name=lang.name, language_rules=f"{lang.rules}\n{lock}",
                              org=ctx.org_name, transfer_hint=transfer_hint)


def inbound_instructions(ctx: CallContext, tools: list[str], prefetched: list[dict[str, Any]]) -> tuple[str, str, str]:
    open_now = ctx.is_open_now()
    greeting = ctx.greeting or f"Bonjour, {ctx.org_name}, je suis l'assistant virtuel. Comment puis-je vous aider ?"
    parts = [
        f"RÔLE : Tu es le réceptionniste virtuel de {ctx.org_name}. Tu réponds aux appels entrants comme un standard "
        "professionnel : tu identifies la demande, tu réponds, tu proposes une action (rendez-vous, message, transfert).",
        f"À PROPOS DE {ctx.org_name.upper()} : {ctx.org_description}" if ctx.org_description else "",
        f"SCRIPT / CONSIGNES DU CLIENT :\n{ctx.script}" if ctx.script else "",
        "STATUT : les bureaux sont actuellement OUVERTS." if open_now else
        "STATUT : les bureaux sont actuellement FERMÉS. Ne propose pas de transfert ; prends un message avec le nom et le numéro de rappel.",
        f"NUMÉRO DE L'APPELANT : {ctx.from_number}" + (f" (client connu : {ctx.contact.get('first_name', '')} {ctx.contact.get('last_name', '')})" if ctx.contact else ""),
        "INTENTIONS À RECONNAÎTRE : faq, appointment, support, complaint, sales, human_request, status_request, contact_info, other. "
        "En fin d'appel, appelle set_outcome avec le résultat.",
        common_rules(ctx, tools),
        knowledge_block(prefetched),
    ]
    kickoff = f"[APPEL_DEBUT] Un appel entrant vient d'être décroché. Dis exactement, naturellement : « {greeting} »"
    return "\n\n".join(p for p in parts if p), kickoff, greeting


def outbound_instructions(ctx: CallContext, tools: list[str], prefetched: list[dict[str, Any]]) -> tuple[str, str, str]:
    c = ctx.contact
    name = " ".join(x for x in [c.get("first_name"), c.get("last_name")] if x) or "l'interlocuteur"
    variables = {**c, "company": ctx.org_name, "org_name": ctx.org_name}
    objective = OBJECTIVE_LABELS.get(ctx.objective, OBJECTIVE_LABELS["custom"])
    parts = [
        f"RÔLE : Tu appelles {name} de la part de {ctx.org_name}. Objectif de l'appel : {objective}.",
        f"DÉTAIL DE L'OBJECTIF : {_fmt(ctx.objective_description, variables)}" if ctx.objective_description else "",
        f"CONTEXTE FOURNI PAR LE CLIENT :\n{_fmt(ctx.context, variables)}" if ctx.context else "",
        f"SCRIPT À SUIVRE (adapte-le naturellement, n'en récite pas des paragraphes) :\n{_fmt(ctx.script, variables)}" if ctx.script else "",
        "DONNÉES DU CONTACT : " + ", ".join(f"{k}={v}" for k, v in c.items() if v not in (None, "") and k != "phone") if c else "",
        "DÉROULÉ :\n"
        f"1. Présente-toi (assistant virtuel de {ctx.org_name}) et vérifie que tu parles bien à {name}. "
        "Si ce n'est pas la bonne personne : set_outcome(status=\"wrong_number\") puis end_call.\n"
        "2. Explique la raison de l'appel en une phrase, puis pose une question ouverte.\n"
        "3. Traite les objections avec empathie (reformule, rassure, apporte un fait issu de la base), sans insister plus de deux fois.\n"
        "4. Propose l'action concrète liée à l'objectif. Confirme les détails.\n"
        "5. Avant de raccrocher, appelle set_outcome (success | callback | not_interested | wrong_number | failed) avec des notes, puis end_call.\n"
        "Si l'interlocuteur n'est pas disponible, propose un rappel (set_outcome status=\"callback\" avec le créneau).",
        common_rules(ctx, tools),
        knowledge_block(prefetched),
    ]
    kickoff = (
        f"[APPEL_DEBUT] {name} vient de décrocher. Présente-toi brièvement (assistant virtuel de {ctx.org_name}) "
        f"et vérifie que tu parles bien à {name}."
    )
    greeting = f"Bonjour, je suis l'assistant virtuel de {ctx.org_name}. Suis-je bien avec {name} ?"
    return "\n\n".join(p for p in parts if p), kickoff, greeting


def voicemail_instruction(ctx: CallContext) -> str:
    msg = ctx.voicemail_message or (
        f"Bonjour, ici l'assistant virtuel de {ctx.org_name}. Nous cherchions à vous joindre, nous vous rappellerons. Bonne journée."
    )
    return f"[REPONDEUR] Tu es tombé sur un répondeur. Laisse exactement ce message, puis appelle end_call : « {_fmt(msg, ctx.contact)} »"
