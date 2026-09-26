from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

_LAT_BUCKETS = (0.05, 0.1, 0.15, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0, 1.5, 2.0, 3.0, 5.0)

ACTIVE_CALLS = Gauge("callwiz_active_calls", "Appels actifs", ["direction"])
CALLS_TOTAL = Counter("callwiz_calls_total", "Appels terminés", ["direction", "status"])
VOICE_TO_VOICE = Histogram("callwiz_voice_to_voice_seconds", "Fin de parole utilisateur → 1er audio IA", buckets=_LAT_BUCKETS)
TOOL_LATENCY = Histogram("callwiz_tool_seconds", "Durée d'exécution des outils agent", ["tool"], buckets=_LAT_BUCKETS)
RAG_LATENCY = Histogram("callwiz_rag_seconds", "Latence retrieval RAG", buckets=(0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0))
LIVE_ERRORS = Counter("callwiz_live_errors_total", "Erreurs session Gemini Live", ["kind"])
SUPERVISOR_ACTIONS = Counter("callwiz_supervisor_actions_total", "Actions du Supervisor", ["action"])
