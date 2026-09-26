"""Langues et voix Gemini Live.

Les modèles Live audio natif choisissent la langue automatiquement : on la verrouille par les instructions
système (méthode recommandée par Google), et optionnellement par `language_code` (LIVE_SEND_LANGUAGE_CODE).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LanguageProfile:
    code: str
    locale: str
    name: str
    rules: str


LANGUAGES: dict[str, LanguageProfile] = {
    "fr": LanguageProfile(
        "fr", "fr-FR", "français",
        "- Parle exclusivement en français de France, avec un accent et des expressions naturels de France métropolitaine.\n"
        "- Vouvoie toujours l'interlocuteur (« vous »), sauf s'il demande explicitement le tutoiement.\n"
        "- Formules naturelles : « Bonjour », « Très bien », « Je vous en prie », « Je vous remercie », « Bonne journée » "
        "(« Bonne soirée » après 18 h). Évite les anglicismes et les tournures traduites de l'anglais.\n"
        "- Heures au format 24 h dites à la française : « quatorze heures trente », jamais « 2 PM ».\n"
        "- Dates : « le mardi 14 octobre ». Prix : « vingt-neuf euros », « quatre-vingt-neuf euros ».\n"
        "- Numéros de téléphone dictés par paires : « zéro six, douze, trente-quatre, cinquante-six, soixante-dix-huit ».\n"
        "- Pour épeler, utilise l'alphabet français (« A comme Anatole, B comme Berthe »).",
    ),
    "en": LanguageProfile(
        "en", "en-US", "English",
        "- Speak only English, in a natural, friendly professional register.\n"
        "- Say times, dates, prices and phone numbers the way a native speaker would.",
    ),
}

# Voix prédéfinies Gemini (mêmes voix que le TTS Gemini ; toutes parlent français).
VOICES: dict[str, str] = {
    "Zephyr": "Bright", "Puck": "Upbeat", "Charon": "Informative", "Kore": "Firm", "Fenrir": "Excitable",
    "Leda": "Youthful", "Orus": "Firm", "Aoede": "Breezy", "Callirrhoe": "Easy-going", "Autonoe": "Bright",
    "Enceladus": "Breathy", "Iapetus": "Clear", "Umbriel": "Easy-going", "Algieba": "Smooth", "Despina": "Smooth",
    "Erinome": "Clear", "Algenib": "Gravelly", "Rasalgethi": "Informative", "Laomedeia": "Upbeat", "Achernar": "Soft",
    "Alnilam": "Firm", "Schedar": "Even", "Gacrux": "Mature", "Pulcherrima": "Forward", "Achird": "Friendly",
    "Zubenelgenubi": "Casual", "Vindemiatrix": "Gentle", "Sadachbia": "Lively", "Sadaltager": "Knowledgeable", "Sulafat": "Warm",
}

# Sélection conseillée pour un centre d'appels français (ton chaleureux, clair, posé).
RECOMMENDED_FR = ["Sulafat", "Achird", "Kore", "Charon", "Aoede", "Schedar", "Iapetus", "Vindemiatrix"]


def language_profile(code: str | None) -> LanguageProfile:
    return LANGUAGES.get((code or "fr").split("-")[0].lower(), LANGUAGES["fr"])


def validate_voice(name: str | None) -> str | None:
    if name in (None, ""):
        return name
    match = next((v for v in VOICES if v.lower() == name.lower()), None)
    if match is None:
        raise ValueError(f"voix inconnue: {name!r}. Voix disponibles : {', '.join(VOICES)}")
    return match
