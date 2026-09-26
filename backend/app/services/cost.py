"""Estimation du coût d'un appel par poste (tarifs configurables en EUR via l'environnement)."""
from __future__ import annotations

from typing import Any

from app.core.config import Settings, get_settings


def estimate_call_cost(duration_s: float, *, answered: bool, usage: dict[str, int] | None = None,
                       settings: Settings | None = None) -> dict[str, Any]:
    s = settings or get_settings()
    minutes = max(duration_s, 0) / 60
    billed_minutes = max(1, int(minutes + 0.999)) if answered else 0  # facturation télécom à la minute entamée
    telephony = billed_minutes * s.cost_telephony_per_min
    # Gemini Live : ~50 % du temps l'IA parle, 100 % du temps elle écoute.
    live_in = minutes * s.cost_live_audio_in_per_min if answered else 0.0
    live_out = minutes * 0.5 * s.cost_live_audio_out_per_min if answered else 0.0
    analysis = s.cost_text_llm_per_call if answered else 0.0
    total = telephony + live_in + live_out + analysis
    return {
        "currency": "EUR",
        "telephony": round(telephony, 5),
        "live_audio_in": round(live_in, 5),
        "live_audio_out": round(live_out, 5),
        "post_call_analysis": round(analysis, 5),
        "tokens": usage or {},
        "total": round(total, 5),
    }
