# API REST — intégration frontend

- Base : `https://<api>/api/v1` (les mêmes routes sont aussi servies sans préfixe, désactivable via `EXPOSE_UNPREFIXED_ROUTES=false`).
- OpenAPI : `/openapi.json`, UI : `/docs`.
- Auth : `Authorization: Bearer <access_token>` (JWT 60 min) ; renouvellement par `POST /auth/refresh`.
- CORS : `*.lovable.app`, `*.lovableproject.com`, `localhost` (regex `CORS_ORIGIN_REGEX`).
- Erreurs uniformes : `{"error": {"code": "not_found", "message": "Campagne introuvable", "details": null}}`
  Codes : `not_authenticated`, `invalid_token`, `token_expired`, `invalid_credentials`, `rate_limited`, `validation_error`, `not_found`, `campaign_running`, `no_contacts`, `invalid_state`, `unsupported_format`, `file_too_large`, `internal_error`.
- Listes paginées : `?limit=50&offset=0` → `{"items": [...], "total": 123, "limit": 50, "offset": 0}`.
- Chaque réponse porte `X-Response-Time-ms`.

## Endpoints

| Méthode | Route | Rôle |
|---|---|---|
| POST | `/auth/register` | `{email, password, full_name?, organization_name}` → tokens (crée l'organisation) |
| POST | `/auth/login` | `{email, password}` → `{access_token, refresh_token, expires_in}` |
| POST | `/auth/refresh` | `{refresh_token}` → tokens |
| GET | `/auth/me` | utilisateur courant |
| GET/PATCH | `/organizations/me` | nom, ton, langue, voix, fuseau, description métier, numéro de transfert |
| GET/POST | `/campaigns` | lister (`?status=`) / créer |
| GET/PATCH/DELETE | `/campaigns/{id}` | détail / modifier (en cours : `name`, `max_concurrency`, `schedule`, `webhook_url` seulement) / supprimer |
| POST | `/campaigns/{id}/start` `…/pause` `…/stop` | pilotage |
| GET | `/campaigns/{id}/progress` | compteurs de progression |
| GET/POST | `/campaigns/{id}/contacts` | lister / ajouter (JSON) |
| POST | `/campaigns/{id}/contacts/import` | multipart `file` (CSV `,` `;` ou tab) + `mapping` JSON optionnel |
| DELETE | `/contacts/{id}` | suppression RGPD (anonymise ses transcripts) |
| GET | `/calls` | `?status=&direction=&campaign_id=&since=` |
| GET | `/calls/{id}` · `/transcript` · `/events` · `/debug` | détail, transcript, timeline, debug (décisions agents, chunks RAG, latences, coût) |
| POST | `/calls/{id}/hangup` | raccrocher |
| POST | `/calls/simulate` | appel simulé texte (démo/test sans téléphone) |
| POST | `/calls/test-outbound` | vrai appel sortant `{to, campaign_id?}` |
| GET/POST | `/phone-numbers` · PATCH/DELETE `/phone-numbers/{id}` | numéros entrants + config agent inbound |
| GET/POST | `/knowledge-bases` · GET `/knowledge-bases/{id}` | bases de connaissances |
| POST | `/knowledge-bases/{id}/documents` | upload (PDF, DOCX, TXT, MD, HTML, CSV) → 202, ingestion asynchrone |
| POST | `/knowledge-bases/{id}/documents/text` | `{filename, content}` texte collé |
| GET | `/documents/{id}/status` · DELETE `/documents/{id}` | `pending → processing → ready | failed` |
| POST | `/knowledge-bases/{id}/test-query` | `{query, top_k}` → chunks, scores, `low_confidence`, latence |
| GET | `/analytics/overview?days=7` | KPIs agrégés |
| GET | `/analytics/realtime` | snapshot temps réel |
| GET | `/analytics/campaigns/{id}` | stats + objections + recommandations |
| GET | `/analytics/calls?days=7&bucket=hour` | série temporelle |
| GET | `/health` · `/metrics` | santé · Prometheus |

## Exemples

```bash
curl -X POST localhost:8000/api/v1/campaigns -H "Authorization: Bearer $T" -H "Content-Type: application/json" -d '{
  "name": "Confirmation RDV", "objective": "confirm_appointment",
  "objective_description": "Confirmer le RDV de {first_name} le {date_rdv}",
  "script": "Rappeler l adresse et proposer de décaler si besoin.",
  "max_concurrency": 20, "schedule": {"days": [0,1,2,3,4], "start": "09:00", "end": "19:00"},
  "voicemail_behavior": "leave_message", "webhook_url": "https://crm.example.com/hooks/callwiz"}'

curl -X POST localhost:8000/api/v1/campaigns/$ID/contacts/import -H "Authorization: Bearer $T" -F file=@examples/contacts.csv
```

Webhook de résultat (POST JSON, par appel) : `event, call_id, campaign_id, contact_id, status, outcome, intent, sentiment, summary, duration_seconds, cost_estimate, objections, messages, appointment`.

Voir `examples/payloads/` et `frontend-adapter/callwiz-sdk.ts` (types TypeScript + client REST/WS prêt à l'emploi).
