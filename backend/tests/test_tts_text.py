from __future__ import annotations

import pytest

from app.voice.tts_text import normalize_for_tts, number_to_words


@pytest.mark.parametrize("n,words", [
    (0, "zéro"), (21, "vingt-et-un"), (71, "soixante-et-onze"), (80, "quatre-vingts"), (81, "quatre-vingt-un"),
    (91, "quatre-vingt-onze"), (200, "deux-cents"), (230, "deux-cent-trente"), (1000, "mille"),
])
def test_number_to_words(n: int, words: str) -> None:
    assert number_to_words(n) == words


@pytest.mark.parametrize("raw,spoken", [
    ("Ouvert de 9h à 19h.", "Ouvert de neuf heures à dix-neuf heures."),
    ("Horaires 9h30-12h.", "Horaires neuf heures trente à midi."),
    ("La révision coûte 89€.", "La révision coûte quatre-vingt-neuf euros."),
    ("29,90 € TTC", "vingt-neuf euros quatre-vingt-dix toutes taxes comprises"),
    ("Rappelez le 06 12 34 56 78.", "Rappelez le zéro six, douze, trente-quatre, cinquante-six, soixante-dix-huit."),
    ("+33 1 23 45 67 89", "zéro un, vingt-trois, quarante-cinq, soixante-sept, quatre-vingt-neuf"),
    ("Votre RDV le 12/03, M. Martin.", "Votre rendez-vous le douze mars, Monsieur Martin."),
    ("Remise de 15 %.", "Remise de quinze pour cent."),
    ("2,5 km", "deux virgule cinq kilomètres"),
    ("Le 1er avril", "Le premier avril"),
    ("**Bonjour** voir https://x.fr", "Bonjour voir notre site internet"),
])
def test_normalize_for_tts(raw: str, spoken: str) -> None:
    assert normalize_for_tts(raw) == spoken
