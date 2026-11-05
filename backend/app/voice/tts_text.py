"""Préparation du texte pour la synthèse vocale française.

Mesuré (aller-retour Fish → Deepgram) : « 9h », « 89€ », « 06 12 34 56 78 » ou « RDV » laissés tels quels font
trébucher la voix ; écrits en toutes lettres, la phrase est comprise en entier. On réécrit donc en mots :
heures, prix, pourcentages, numéros de téléphone (par paires), dates, nombres, abréviations ; et on retire le
balisage (markdown, émojis, URL) que le modèle de langue peut produire.
"""
from __future__ import annotations

import re

_UNITS = ["zéro", "un", "deux", "trois", "quatre", "cinq", "six", "sept", "huit", "neuf", "dix", "onze", "douze",
          "treize", "quatorze", "quinze", "seize", "dix-sept", "dix-huit", "dix-neuf"]
_TENS = {2: "vingt", 3: "trente", 4: "quarante", 5: "cinquante", 6: "soixante"}
_MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"]


def _below_100(n: int) -> str:
    if n < 20:
        return _UNITS[n]
    tens, unit = divmod(n, 10)
    if tens in (7, 9):  # soixante-dix…, quatre-vingt-dix…
        base = "soixante" if tens == 7 else "quatre-vingt"
        rest = 10 + unit
        return f"{base}-et-onze" if (tens == 7 and unit == 1) else f"{base}-{_UNITS[rest]}"
    if tens == 8:
        return "quatre-vingts" if unit == 0 else f"quatre-vingt-{_UNITS[unit]}"
    word = _TENS[tens]
    if unit == 0:
        return word
    return f"{word}-et-un" if unit == 1 else f"{word}-{_UNITS[unit]}"


def number_to_words(n: int) -> str:
    """Entier → mots (orthographe réformée, traits d'union), jusqu'aux milliards."""
    if n < 0:
        return "moins " + number_to_words(-n)
    if n < 100:
        return _below_100(n)
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        head = "cent" if hundreds == 1 else f"{_UNITS[hundreds]}-cent" + ("s" if rest == 0 else "")
        return head if rest == 0 else f"{head}-{_below_100(rest)}"
    for value, name in ((1_000_000_000, "milliard"), (1_000_000, "million"), (1000, "mille")):
        if n >= value:
            high, rest = divmod(n, value)
            if name == "mille":
                head = "mille" if high == 1 else f"{number_to_words(high).removesuffix('s') if high % 100 == 0 else number_to_words(high)}-mille"
            else:
                head = f"{number_to_words(high)} {name}" + ("s" if high > 1 else "")
            return head if rest == 0 else f"{head} {number_to_words(rest)}"
    return str(n)


def _decimal(int_part: str, dec_part: str | None) -> str:
    words = number_to_words(int(int_part))
    if dec_part:  # 2,5 → « deux virgule cinq » ; 1,05 → « un virgule zéro cinq »
        words += " virgule " + (number_to_words(int(dec_part)) if not dec_part.startswith("0") else " ".join(_UNITS[int(c)] for c in dec_part))
    return words


def _price(m: re.Match[str]) -> str:
    euros, cents = m.group("int"), m.group("dec")
    out = f"{number_to_words(int(euros.replace(' ', '')))} euro" + ("s" if int(euros.replace(' ', '')) > 1 else "")
    if cents and int(cents):
        out += f" {number_to_words(int(cents.ljust(2, '0')[:2]))}"
    return out


def _time(m: re.Match[str]) -> str:
    h, mins = int(m.group("h")), m.group("m")
    out = "midi" if h == 12 and not mins else ("minuit" if h in (0, 24) and not mins else f"{number_to_words(h)} heure" + ("s" if h > 1 else ""))
    if mins and int(mins):
        out += f" {number_to_words(int(mins))}"
    return out


def _phone(m: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", m.group(0))
    if digits.startswith("33") and len(digits) == 11:
        digits = "0" + digits[2:]
    if len(digits) != 10:
        return " ".join(_UNITS[int(d)] for d in digits)
    pairs = [digits[i : i + 2] for i in range(0, 10, 2)]
    spoken = [("zéro " + _UNITS[int(p[1])]) if p[0] == "0" else number_to_words(int(p)) for p in pairs]
    return ", ".join(spoken)


def _date(m: re.Match[str]) -> str:
    day, month = int(m.group("d")), int(m.group("mo"))
    if not (1 <= day <= 31 and 1 <= month <= 12):
        return m.group(0)
    out = f"{'premier' if day == 1 else number_to_words(day)} {_MONTHS[month - 1]}"
    if m.group("y"):
        y = int(m.group("y"))
        out += " " + number_to_words(y + 2000 if y < 100 else y)
    return out


ABBREVIATIONS = [
    (r"\bRDV\b", "rendez-vous"), (r"\brdv\b", "rendez-vous"), (r"\bM\.(?=\s+[A-ZÉ])", "Monsieur"), (r"\bMme\b", "Madame"),
    (r"\bMlle\b", "Mademoiselle"), (r"\bn°\s*", "numéro "), (r"\bN°\s*", "numéro "), (r"\bTTC\b", "toutes taxes comprises"),
    (r"\bHT\b", "hors taxes"), (r"\bSAV\b", "service après-vente"), (r"\betc\.", "et cetera"), (r"\bex\.", "par exemple"),
    (r"\bmin\b", "minutes"), (r"\bkm\b", "kilomètres"), (r"\bkg\b", "kilos"), (r"\bh\b", "heures"), (r"&", " et "),
    (r"\bSMS\b", "S M S"), (r"\bCB\b", "carte bancaire"), (r"\bIBAN\b", "I BAN"),
]

_MARKDOWN = [(re.compile(r"\*\*|__|`+|#+\s*"), ""), (re.compile(r"(?m)^\s*[-*•]\s+"), ""), (re.compile(r"\[([^\]]+)\]\([^)]+\)"), r"\1")]
_URL = re.compile(r"https?://\S+|www\.\S+")
_EMAIL = re.compile(r"\b([\w.+-]+)@([\w-]+)\.([a-z]{2,})\b", re.I)
_EMOJI = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF]")
_PHONE = re.compile(r"(?:\+33\s?|0)[1-9](?:[\s.-]?\d{2}){4}\b")
_TIME = re.compile(r"\b(?P<h>[01]?\d|2[0-4])\s?[hH](?:\s?(?P<m>[0-5]\d))?\b")
_TIME_RANGE = re.compile(r"(\d{1,2}\s?[hH](?:\s?\d{2})?)\s?[-–]\s?(\d{1,2}\s?[hH])")
_PRICE = re.compile(r"(?P<int>\d{1,3}(?:[  ]\d{3})*|\d+)(?:[,.](?P<dec>\d{1,2}))?\s?(?:€|euros?\b|EUR\b)")
_PERCENT = re.compile(r"(\d+)(?:[,.](\d+))?\s?%")
_DATE = re.compile(r"\b(?P<d>\d{1,2})/(?P<mo>\d{1,2})(?:/(?P<y>\d{2,4}))?\b")
_ORDINAL = re.compile(r"\b(\d+)(?:er|ère|e|ème)\b")
_DECIMAL = re.compile(r"\b(\d+)[,.](\d+)\b")
_INT = re.compile(r"\b\d{1,9}\b")


def normalize_for_tts(text: str) -> str:
    if not text:
        return text
    for rx, rep in _MARKDOWN:
        text = rx.sub(rep, text)
    text = _URL.sub("notre site internet", text)
    text = _EMAIL.sub(lambda m: f"{m.group(1)} arobase {m.group(2)} point {m.group(3)}", text)
    text = _EMOJI.sub("", text)
    text = _PHONE.sub(_phone, text)
    text = _TIME_RANGE.sub(r"\1 à \2", text)
    text = _PRICE.sub(_price, text)
    text = _PERCENT.sub(lambda m: f"{_decimal(m.group(1), m.group(2))} pour cent", text)
    text = _DATE.sub(_date, text)
    text = _TIME.sub(_time, text)
    text = _ORDINAL.sub(lambda m: "premier" if m.group(1) == "1" else number_to_words(int(m.group(1))) + "ième", text)
    text = _DECIMAL.sub(lambda m: _decimal(m.group(1), m.group(2)), text)
    for pattern, rep in ABBREVIATIONS:
        text = re.sub(pattern, rep, text)
    text = _INT.sub(lambda m: number_to_words(int(m.group(0))), text)
    return re.sub(r"\s{2,}", " ", text).strip()
