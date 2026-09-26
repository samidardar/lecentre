# CallWiz AI — Rapport d'implémentation du MVP

*Date : 26 septembre 2026 · Périmètre : backend MVP complet (phases 1 à 9 du PRD) · État : fonctionnel en mode simulé, prêt à brancher Gemini Live + Twilio.*

## 1. Résumé

Le backend MVP de CallWiz AI est implémenté de bout en bout : API REST + WebSocket pour le frontend Lovable, quatre agents (Supervisor, Inbound, Outbound, Analytics) orchestrés par **LangGraph**, voix temps réel **Gemini Live en audio natif**, **RAG hybride** (Qdrant + BM25), télécom **Twilio** abstraite, dispatcher de campagnes avec backpressure, analytics temps réel, CLI, serveurs MCP, SDK TypeScript, Docker et documentation.

| Critère du PRD | Résultat |
|---|---|
| 100 appels simultanés | ✅ Load test : 100 appels, **0 échec**, pic de concurrence **100**, 22,6 s |
| Latence voix → voix | ✅ Overhead plateforme ≈ **0 ms** mesuré (p50 = 250 ms = délai simulé du modèle, p95 = 297 ms) |
| RAG sans hallucination | ✅ Réponses tirées de la base ; `low_confidence` → pas d'invention ; détection des chiffres non sourcés |
| Isolation multi-tenant | ✅ Tous les accès filtrés par organisation (testé) |
| Tests | ✅ **27/27** tests (API, RAG, agents, Supervisor, Twilio, barge-in, campagnes, charge) |
| Coût estimé par appel | ✅ Calculé par poste et exposé (≈ 0,01 € pour un appel simulé d'environ 30 s, avec les tarifs par défaut) |

**Limite principale :** aucune clé Gemini ni Twilio n'était disponible pendant le développement. Les intégrations réelles sont écrites d'après la documentation officielle actuelle et testées au niveau des composants (signature Twilio, protocole Media Streams, conversions audio), mais **aucun vrai appel n'a encore été passé**. C'est la première étape de la feuille de route (§8).

## 2. Décisions structurantes

1. **Gemini Live en audio natif (`gemini-3.8-live`).** Une seule connexion remplace STT + LLM + TTS. Le VAD, la prise de parole et le barge-in sont natifs. Les transcriptions d'entrée et de sortie sont activées. Pour les appels longs, le contexte est compressé (fenêtre glissante) et la session reprend d'elle-même après un `go_away`.
2. **LangGraph comme plan de contrôle, jamais sur le chemin de l'audio.** Quatre graphes sont compilés au démarrage :
   - `call_plan` : config → routage Supervisor → prefetch RAG → agent Inbound ou Outbound ;
   - `tool` : autorisation → exécution ou refus → journalisation ;
   - `turn` : supervision de chaque tour, en arrière-plan ;
   - `post_call` : analyse LLM → coût → persistance → webhook et événement.
3. **La supervision s'adapte à l'audio natif.** La réponse est prononcée avant qu'on puisse lire son texte : on ne peut donc pas la valider avant la synthèse vocale. Le Supervisor agit donc :
   - **en amont** : instructions strictes, RAG obligatoire via un outil, outils autorisés selon la configuration ;
   - **à chaud** : à chaque fin de tour, il détecte l'opt-out, les demandes d'humain, la frustration, les boucles, les réponses trop longues, l'absence d'annonce IA, la durée maximale et les **chiffres, prix ou délais absents des sources**. Il injecte alors une correction immédiate, transfère ou termine l'appel.
4. **RAG embarqué par défaut.** Qdrant en mode local, sans serveur, avec des embeddings `fastembed` locaux (aucun appel réseau pendant un appel). La recherche est hybride (dense + BM25, fusion RRF) et filtrée par organisation et par base de connaissances.
5. **Mock-first.** Tout tourne sans infrastructure ni clé (SQLite, bus mémoire, Gemini et Twilio simulés). Le passage en réel se fait uniquement par `.env`.

Le détail est dans [docs/ADR.md](docs/ADR.md).

## 3. Architecture livrée

```
Twilio ⇄ TwilioMediaTransport (µ-law 8k ↔ PCM 16k/24k, clear/mark)
             ⇅
        CallSession (1 tâche asyncio isolée par appel)
          ├─ pump_in  : audio → Gemini Live (+ VAD énergie pour métriques/silences)
          ├─ pump_out : audio → Twilio · transcriptions · interruptions · tool calls · fin de tour
          ├─ watchdog : silences (relance → proposition de rappel → fin polie), durée max
          └─ graphes LangGraph : call_plan · tool · turn (fond) · post_call
             ⇅
   Bus d'événements (mémoire | Redis) → AnalyticsAgent (persistance par lots 250 ms, compteurs) → Hub WS → dashboard
   CampaignDispatcher → CallManager → TelephonyProvider (Twilio | mock)
```

## 4. Ce qui est implémenté (par module du PRD)

| Module | Implémentation |
|---|---|
| Organisations & auth | JWT access/refresh, bcrypt, création d'organisation à l'inscription, rate limiting sur le login, isolation par organisation |
| Campagnes outbound | CRUD, import CSV (détection des colonnes, `,` `;` tab, normalisation E.164, dédoublonnage, attributs libres → variables `{…}` du script), start/pause/stop, progression |
| Dispatcher | Plafond global compté en base (multi-process), plafond par campagne, token bucket (appels/s), fenêtre horaire dans le fuseau de l'organisation, relances avec délai, reprise après redémarrage |
| Outbound Agent | Vérification d'identité, objectifs du PRD, objections, répondeur (AMD asynchrone Twilio → message ou raccrochage), `set_outcome` obligatoire, opt-out persistant |
| Inbound Agent | Accueil personnalisé, horaires ouvert/fermé, intents du PRD, RAG, prise de message, RDV, transfert (`<Dial>`) |
| Supervisor Agent | Routage, autorisation des outils, supervision de chaque tour (8 règles), décisions journalisées (`AgentDecision`) |
| Analytics Agent | Analyse post-appel (Gemini Flash-Lite en JSON structuré, repli heuristique), coût, compteurs temps réel, `analytics.updated` limité à 2 par seconde |
| RAG | Formats PDF, DOCX, HTML, TXT/MD, CSV FAQ ; chunking par section et par paire Q/R (250–600 tokens) ; hybride + RRF ; cache TTL ; seuil de confiance ; endpoint `test-query` ; chunks utilisés visibles dans `/calls/{id}/debug` |
| Voix | Gemini Live, barge-in, silences, transcriptions, reprise de session, latence voix→voix mesurée à chaque tour (Prometheus + base) |
| Télécom | Twilio en REST pur (httpx, circuit breaker, retries), TwiML, Media Streams, signature HMAC, webhooks voice/status/amd |
| Analytics | `/overview`, `/realtime`, `/campaigns/{id}` (objections, raisons d'échec, recommandations), `/calls` (séries temporelles), debug par appel |
| Frontend | OpenAPI (39 routes), routes avec préfixe `/api/v1` et sans préfixe, erreurs uniformes, CORS Lovable, 3 canaux WS, SDK TS avec reconnexion et refresh du token |
| MCP | Client générique (stdio et HTTP) : les outils des serveurs configurés deviennent des outils d'agent. Exemples fournis : CRM, calendrier, devtools pour Claude Code. **Testé avec le SDK mcp 2.2** |
| CLI | `dev`, `worker`, `init`, `db`, `campaign`, `call test`, `rag`, `monitor live`, `analytics tail`, `loadtest`, `gdpr purge` |
| Sécurité & RGPD | Annonce IA imposée, opt-out, redaction des logs (cartes, IBAN, emails, numéros, secrets), suppression d'un contact → anonymisation, purge par rétention |
| Observabilité | Logs JSON avec `call_id` et `organization_id`, `/metrics` Prometheus, timeline complète des événements, en-tête `X-Response-Time-ms` |

## 5. Résultats de validation

**Tests automatisés :** `pytest` passe **27/27** en 30 s. Couverture :
- auth et isolation des tenants ;
- CRUD et import CSV ;
- ingestion RAG puis requête, et cas hors sujet → `low_confidence` ;
- appel entrant simulé de bout en bout (réponse issue du RAG, annonce IA, latence, coût, décisions) ;
- règles du Supervisor (opt-out, hallucination « 3 jours / 12 euros » détectée, annonce IA, escalade) ;
- graphe d'outils (refus / autorisation / RAG vide) ;
- chunking et parsing ;
- signature et TwiML Twilio, transport Media Streams, conversions audio et VAD, barge-in ;
- campagne complète avec événements ;
- pause / reprise ;
- **100 appels simultanés**.

**Démo CLI (`callwiz call test`) :**
```
🤖 Bonjour, VéloCity Paris, je suis l'assistant virtuel. Comment puis-je vous aider ?
   🔧 search_knowledge_base({"query": "…horaires d'ouverture ?"}) 2.9 ms
👤 Bonjour, quels sont vos horaires d'ouverture ?
🤖 Le magasin est ouvert du lundi au samedi de 9h à 19h. …
👤 Et combien coûte une révision ?
🤖 La révision complète coûte 89 euros et prend environ 2 heures. …
   🔧 end_call({"reason": "fin de conversation"})
→ status completed · outcome success · intent contact_info · coût 0,0094 € · latence moyenne 258 ms
```

**Load test (`callwiz loadtest --calls 100`, modèle simulé à 250 ms) :**
```json
{"calls": 100, "failed": 0, "peak_concurrency": 100, "elapsed_s": 22.6,
 "voice_to_voice_ms": {"p50": 250.0, "p95": 297.0, "samples": 357}}
```
Le p50 est égal au délai simulé du modèle : la plateforme n'ajoute pas de latence mesurable sous 100 appels. En réel, la latence sera celle de Gemini Live, plus environ 20–60 ms de réseau Twilio.

**RAG :**
- recherche hybride en 13–18 ms en local ;
- ingestion de la FAQ de démo : 9 chunks Q/R intacts ;
- MCP : outils CRM et calendrier découverts et appelés depuis le backend.

## 6. Bugs trouvés et corrigés pendant la validation
- `tzdata` manquant sous Windows : `ZoneInfo("Europe/Paris")` échouait, ce qui cassait les horaires et le dispatcher. La dépendance est ajoutée.
- Argument `campaign_id` dupliqué dans l'événement `campaign.progress` : la campagne passait en `failed`. Corrigé.
- SDK MCP 2.x : `FastMCP` est renommé `MCPServer`, les champs passent en snake_case et `streamable_http_client` change de nom. Le code est compatible 1.x et 2.x.

## 7. Limites connues (à traiter)
1. **Pas encore d'appel réel** Gemini Live / Twilio (pas de clés). À valider en priorité : format audio, qualité de la voix française, comportement d'interruption, AMD.
2. **Supervision a posteriori** avec l'audio natif : une erreur peut être prononcée avant d'être corrigée. Elle est détectée et corrigée au tour suivant, et le prompt comme le RAG forcé la rendent rare. Si un client exige une validation préalable stricte (santé, finance), il faudra un mode cascade texte → TTS (l'abstraction `LiveModel` est prévue pour ça).
3. **Embeddings par défaut (MiniLM multilingue) faibles sur les paraphrases** (« rendre mon vélo » ≠ « politique de retour »). BM25 compense en partie. Recommandation : `FASTEMBED_MODEL=google/embeddinggemma-300m` ou `EMBEDDER=gemini`, et un reranker.
4. **Tarifs de coût = valeurs par défaut configurables** : à recaler sur les grilles Twilio FR et Gemini Live en vigueur. La cible de 0,10 €/appel est tenable pour des appels de 3–4 minutes avec les valeurs par défaut.
5. **Frontend non modifié** : le SDK TS et les contrats sont fournis, mais le branchement des écrans Lovable reste à faire.
6. SQLite suffit pour la démo et le test de charge simulé. **PostgreSQL + Redis sont requis en production multi-instance** (fournis dans docker-compose).
7. Le prompt caching et la sélection de voix par langue ne sont pas exploités. Le moteur Live choisit la langue automatiquement.

## 8. Feuille de route recommandée
1. **Semaine 1 :** clés Gemini et Twilio, tunnel, puis 20 appels réels internes. Mesures : latence réelle, taux d'interruptions intempestives, réglage de `LIVE_SILENCE_DURATION_MS` et des sensibilités VAD.
2. Brancher le frontend Lovable via `frontend-adapter/callwiz-sdk.ts` : dashboard WS, campagnes, debug d'appel.
3. RAG : embeddings de meilleure qualité, reranker, et évaluation sur un jeu de 100 questions par client.
4. Production : PostgreSQL, Redis, Qdrant cluster, 2 instances API derrière un load balancer avec WebSocket sticky, alertes Prometheus (latence p95, `callwiz_live_errors_total`).
5. Conformité : registre des appels, consentement outbound, consultation Bloctel à l'import.
6. Agents suivants (vente, recouvrement, support) : une classe `plan()` et une règle de routage chacun (voir ADR-002).

## 9. Lancer
Voir [README.md](README.md). En résumé :
```bash
python -m venv backend/.venv
backend/.venv/Scripts/python -m pip install -e "backend[dev,docs,mcp,embeddings]"
cd backend
callwiz db migrate
callwiz db seed
callwiz call test
callwiz loadtest --calls 100
callwiz dev
```
