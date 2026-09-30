"""Langues et voix Gemini Live.

Les modèles Live audio natif choisissent la langue automatiquement : on la verrouille par les instructions
système (méthode recommandée par Google), et optionnellement par `language_code` (LIVE_SEND_LANGUAGE_CODE).
"""
from __future__ import annotations

import re
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


FISH_VOICE_ID = re.compile(r"^[0-9a-f]{32}$")

# Voix françaises Fish Audio présélectionnées pour un centre d'appels (bibliothèque publique ; aucune voix
# imitant une personne réelle). reference_id → (nom, style).
FISH_FR_VOICES: dict[str, tuple[str, str]] = {
    "5567200c7d8341738f0892bbacd3be3c": ("Féminine", "femme, naturelle, conversationnelle"),
    "a288bdc744da4ad194921adad6863175": ("Clémence", "femme, grave, posée"),
    "10a3a20742114a4ea6dd441e7591850f": ("Manon", "femme, claire, service client"),
    "0638c82cfa894e8fb71f42487f69e050": ("Narratrice claire", "femme, claire, informative"),
    "6e10fb8946b34ba6bec447789ccdc3de": ("Stoïc", "homme, calme, professionnel"),
    "f69bca092b674168a8d02d61ca20943c": ("Lucas", "homme, clair, assistant vocal"),
}


def validate_voice(name: str | None) -> str | None:
    """Accepte une voix Gemini (par nom) ou une voix Fish Audio (reference_id de 32 caractères hexadécimaux)."""
    if name in (None, ""):
        return name
    if FISH_VOICE_ID.match(name.lower()):
        return name.lower()
    match = next((v for v in VOICES if v.lower() == name.lower()), None)
    if match is None:
        raise ValueError(f"voix inconnue: {name!r} (attendu : id Fish Audio de 32 caractères, ou voix Gemini : {', '.join(VOICES)})")
    return match
