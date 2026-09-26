# CallWiz AI — centre d'appels IA temps réel

Plateforme SaaS B2B : **standard téléphonique IA (inbound)** et **campagnes d'appels sortants (outbound)** avec voix native
**Gemini Live**, orchestration **LangGraph**, **RAG** hybride sur les documents du client, supervision qualité et analytics temps réel.

```
Frontend (Lovable) ──REST /api/v1 + WebSocket /ws/*──▶ FastAPI
                                                      │
        Twilio (PSTN) ◀─Media Streams µ-law 8k─▶ CallSession ◀─audio PCM 16k/24k─▶ Gemini Live (gemini-3.8-live)
                                                      │  transcriptions · tool calls · interruptions
                                LangGraph (plan de contrôle) : call_plan · tool · turn · post_call
                                   │ Supervisor · Inbound · Outbound · Analytics agents
                                   ├── RAG : Qdrant (embarqué ou cluster) + BM25 + RRF, fastembed
                                   ├── Outils : KB, RDV, message, transfert, CRM, MCP servers
                                   └── Bus d'événements (mémoire | Redis) → Analytics → WebSocket → dashboard
```

📄 **Rapport complet : [REPORT.md](REPORT.md)** — architecture, choix, résultats de tests et de charge, limites, feuille de route.

## Démarrage rapide (sans Docker, sans clé API)

```bash
python -m venv backend/.venv
backend/.venv/Scripts/python -m pip install -e "backend[dev,docs,mcp,embeddings]"   # Linux/macOS : backend/.venv/bin/…
cd backend
callwiz init                 # crée .env (secret JWT aléatoire)
callwiz db migrate
callwiz db seed              # org démo « VéloCity Paris » + FAQ + numéro + campagne de 20 contacts
callwiz call test --agent inbound      # conversation simulée affichée en direct (outils, latences)
callwiz rag query "Quelle est votre politique de retour ?"
callwiz loadtest --calls 100           # 100 appels simultanés (mock) : 0 échec, p95 ≈ latence du modèle
callwiz dev                            # API sur http://localhost:8000 — docs : /docs
```

Par défaut tout est **simulé** (`LIVE_PROVIDER=mock`, `TELEPHONY_PROVIDER=mock`) : SQLite, bus mémoire, Qdrant embarqué.
Identifiants de la démo : voir `SEED_ADMIN_EMAIL` / `SEED_ADMIN_PASSWORD` dans [.env.example](.env.example).

## Passer en réel
```
LIVE_PROVIDER=gemini          GEMINI_API_KEY=…
TELEPHONY_PROVIDER=twilio     TWILIO_ACCOUNT_SID=…  TWILIO_AUTH_TOKEN=…  TWILIO_DEFAULT_FROM=+33…
PUBLIC_BASE_URL=https://<tunnel-ou-domaine>
```
Puis `callwiz call test --to +336XXXXXXXX` pour recevoir l'agent sur votre téléphone. Détails : [docs/TELEPHONY.md](docs/TELEPHONY.md).
Production (PostgreSQL + Redis + Qdrant + worker) : `docker compose up --build`.

## Commandes CLI
| Commande | Rôle |
|---|---|
| `callwiz dev` / `callwiz worker` | API complète / dispatcher de campagnes seul (scale-out) |
| `callwiz db migrate` · `db seed` | migrations Alembic · données de démo |
| `callwiz campaign create/list/launch --file contacts.csv --concurrency 100/pause/stop` | campagnes |
| `callwiz call test [--agent inbound\|outbound] [--say "…"]... [--to +33…]` | appel simulé ou réel |
| `callwiz rag ingest --file faq.pdf` · `rag query "…"` | base de connaissances |
| `callwiz monitor live` · `analytics tail` | flux temps réel d'une API en cours |
| `callwiz loadtest --calls 100` | test de charge |
| `callwiz gdpr purge` | purge RGPD (RETENTION_DAYS) |

## Documentation
- [docs/CONFIGURATION.md](docs/CONFIGURATION.md) — APIs à créer, `.env`, français et voix
- [docs/API.md](docs/API.md) — endpoints, erreurs, pagination, exemples
- [docs/WEBSOCKET_EVENTS.md](docs/WEBSOCKET_EVENTS.md) — contrat des événements temps réel
- [docs/ADR.md](docs/ADR.md) — décisions d'architecture
- [docs/TELEPHONY.md](docs/TELEPHONY.md) — Twilio, AMD, transfert, conformité
- [frontend-adapter/](frontend-adapter/) — SDK TypeScript (types + client REST/WS avec reconnexion)
- [mcp/](mcp/) — serveurs MCP (CRM, calendrier, devtools pour Claude Code)
- [.env.example](.env.example) — toutes les variables d'environnement

## Tests
```bash
cd backend && .venv/Scripts/python -m pytest -q     # 28 tests : API, isolation multi-tenant, RAG, agents, Supervisor,
                                                    # Twilio (signature, media), barge-in, campagne, 100 appels simultanés
```

## Structure
```
backend/app/
  api/         routes REST, WebSocket, webhooks Twilio
  agents/      supervisor, inbound, outbound, analytics, tools, graphs (LangGraph), prompts, mcp_client
  live/        Gemini Live (audio natif) + modèle simulé
  voice/       CallSession (boucle temps réel), transports (Twilio, simulé), conversions audio
  telephony/   Twilio (REST + TwiML + signature), mock
  rag/         parsing, chunking, embeddings, store (Qdrant/mémoire), retrieval hybride, service
  services/    call_manager, dispatcher (backpressure), import CSV, coûts
  core/        config, logs JSON + redaction, sécurité JWT, bus d'événements, métriques, résilience
backend/cli/   CLI Typer        backend/tests/   pytest        backend/alembic/   migrations
```
