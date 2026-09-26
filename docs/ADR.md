# Décisions d'architecture (ADR)

## ADR-001 — Gemini Live en audio natif (speech-to-speech)
**Décision** : `gemini-3.8-live` via l'API Live (`google-genai`, `client.aio.live.connect`), réponse en `AUDIO`.
**Pourquoi** : une seule connexion remplace STT + LLM + TTS → pas de latence cumulée entre trois fournisseurs ; VAD, turn-taking et barge-in sont natifs ; prosodie plus naturelle qu'un TTS de phrases.
**Conséquences** :
- Le texte de la réponse n'existe pas *avant* d'être prononcé → pas de validation pré-TTS. La supervision est donc (1) préventive : instructions strictes, RAG forcé par outil, outils autorisés par le Supervisor ; (2) corrective : analyse de chaque tour (transcriptions) et correction immédiate (`[SUPERVISEUR] …` injecté, transfert ou fin).
- Transcriptions entrée/sortie activées pour transcripts, supervision et analytics.
- Sessions longues : compression de contexte (sliding window) + reprise transparente (`session_resumption`, `go_away`).
- `LiveModel` est une abstraction : un pipeline cascade (STT→LLM→TTS) peut être ajouté comme autre implémentation sans toucher aux agents.

## ADR-002 — LangGraph comme plan de contrôle, jamais dans le chemin audio
Quatre graphes compilés au démarrage : `call_plan`, `tool`, `turn`, `post_call` (voir `app/agents/graphs.py`).
L'audio Twilio ⇄ Gemini ne traverse aucun nœud. Les graphes s'exécutent au décroché (< 50 ms, prefetch RAG borné à 800 ms), sur appel d'outil (Gemini attend de toute façon le résultat) et en fin de tour **en tâche de fond**.
Ajouter un agent (vente, recouvrement…) = une classe `plan()` enregistrée dans `AGENTS` + une branche de routage du Supervisor.

## ADR-003 — RAG embarqué par défaut
Qdrant en mode local (`QdrantClient(path=…)`) : zéro serveur en dev, même API qu'un cluster (`QDRANT_URL`).
Embeddings `fastembed` locaux (≈ 5–15 ms/requête, aucun appel réseau pendant l'appel). Recherche hybride dense + BM25 fusionnée par RRF, filtrée par `organization_id` (et KB). `low_confidence` si ni la similarité dense ni la couverture lexicale ne passent le seuil → l'agent reçoit l'instruction de ne pas inventer.
Les paires Q/R et sections sont préservées au chunking (250–600 tokens).

## ADR-004 — SQLite par défaut, PostgreSQL en production
SQLAlchemy async + Alembic : même code. SQLite en WAL suffit pour la démo et 100 appels mock ; PostgreSQL recommandé en prod (pool 20+40).
Les écritures d'événements sont regroupées (flush toutes les 250 ms) par l'Analytics Agent → la base n'est jamais sur le chemin critique d'un appel.

## ADR-005 — Pas de Celery : dispatcher asyncio + base comme source de vérité
Le `CampaignDispatcher` applique la backpressure (plafond global compté en base, plafond par campagne, token bucket CPS, fenêtre horaire, relances). L'état est reconstructible au redémarrage. Scale-out : `callwiz worker` (dispatcher) + N instances API (médias) avec `REDIS_URL` pour le bus.

## ADR-006 — Télécom abstraite
`TelephonyProvider` (make_call, hangup, transfer) + `MediaTransport` (receive, send_audio, clear). Twilio implémenté en REST pur (httpx, circuit breaker, retries) + Media Streams bidirectionnels ; provider mock pour tests et démo. Telnyx/SIP s'ajoutent sans toucher aux agents.

## ADR-007 — Python 3.10+
Le poste de dev est en 3.10 ; le code évite les API 3.11+ (`TaskGroup`, `except*`). `audioop` est fourni par `audioop-lts` à partir de 3.13.
