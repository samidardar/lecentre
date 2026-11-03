# CallWiz AI — centre d'appels IA temps réel

Plateforme SaaS B2B : **standard téléphonique IA (inbound)** et **campagnes d'appels sortants (outbound)** en français
naturel. Pipeline vocal **Deepgram (écoute) → LLM via OpenRouter (Claude Haiku 5.5 par défaut) → Fish Audio (voix)**,
orchestration **LangGraph**, **RAG** hybride sur les documents du client, supervision qualité, analytics temps réel,
**console web intégrée** et **appel test depuis le navigateur**.

```
Téléphone (Twilio) ─┐                                   ┌─ Deepgram Nova-3 (transcription streaming)
Navigateur (micro) ─┼─▶ CallSession (1 tâche / appel) ──┼─ LLM OpenRouter (streaming + outils)
                    │    LangGraph : plan · outils ·    └─ Fish Audio (voix streaming, préouverte)
Console /console ◀──┘    supervision · analyse finale
                         RAG (Qdrant + BM25) · bus d'événements → analytics → WebSocket
```

📄 **Rapport complet : [REPORT.md](REPORT.md)** · ⚙️ **Clés et `.env` : [docs/CONFIGURATION.md](docs/CONFIGURATION.md)**

## Démarrage rapide (sans Docker, sans clé API)

```bash
python -m venv backend/.venv
backend/.venv/Scripts/python -m pip install -e "backend[dev,docs,mcp,embeddings]"   # Linux/macOS : backend/.venv/bin/…
cd backend
callwiz init          # crée .env (secret JWT aléatoire)
callwiz db migrate
callwiz db seed       # org démo « VéloCity Paris » + FAQ + numéro + campagne de 20 contacts
callwiz dev           # puis ouvrez http://localhost:8000/console
```

Par défaut tout est **simulé** (`LIVE_PROVIDER=mock`, `TELEPHONY_PROVIDER=mock`) : la console, les appels navigateur,
les campagnes et les analytics fonctionnent sans aucune clé. Identifiants de la démo : `SEED_ADMIN_EMAIL` /
`SEED_ADMIN_PASSWORD` dans [.env.example](.env.example).

## Passer en réel

1. Renseignez dans `backend/.env` : `OPENROUTER_API_KEY`, `DEEPGRAM_API_KEY`, `FISH_API_KEY`, `TWILIO_ACCOUNT_SID`,
   `TWILIO_AUTH_TOKEN`, puis `LIVE_PROVIDER=cascade` et `TELEPHONY_PROVIDER=twilio`.
2. `callwiz doctor` — vérifie chaque clé et service et dit quoi faire s'il manque quelque chose (`--live` : mesure la
   latence réelle du LLM et teste la synthèse vocale).
3. `callwiz up` — ouvre un tunnel HTTPS public (cloudflared dans `tools/`), met à jour `PUBLIC_BASE_URL`, relie votre
   numéro Twilio au serveur et démarre l'API. Appelez votre numéro, ou testez au micro dans `/console`.

Hébergement : [render.yaml](render.yaml) (Render, API + PostgreSQL en un clic) ou `docker compose up --build`.

## Commandes CLI
| Commande | Rôle |
|---|---|
| `callwiz up` | tunnel public + numéro Twilio relié + serveur, en une commande |
| `callwiz doctor [--live]` | diagnostic de toutes les clés et de tous les services |
| `callwiz twilio connect [--number +…]` | relie un numéro Twilio au serveur |
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
