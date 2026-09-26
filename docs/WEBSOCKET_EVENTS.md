# Événements temps réel (contrat WebSocket)

## Connexion

| Canal | URL | Contenu |
|---|---|---|
| Appels | `wss://<api>/ws/calls?token=<access_token>` | tous les `call.*` de l'organisation |
| Campagne | `wss://<api>/ws/campaigns/{id}?token=…` | `campaign.progress`, `campaign.status` + `call.*` de la campagne |
| Analytics | `wss://<api>/ws/analytics?token=…` | snapshot initial puis `analytics.updated` (≤ 2/s) |

- Premier message : `{"event":"connected","payload":{"channel":"calls","organization_id":"…"}}`
- Auth invalide : `{"event":"error","payload":{"code":"invalid_token",…}}` puis fermeture code `4401`.
- Heartbeat serveur : `{"event":"ping"}` toutes les 20 s. Le client peut envoyer `{"type":"ping"}` → `{"event":"pong"}`.
- Un client lent perd des événements (file de 1000) plutôt que de ralentir les appels : recharger via REST si besoin.

## Enveloppe

```json
{
  "id": "5f0c…",
  "event": "call.response_generated",
  "organization_id": "uuid",
  "call_id": "uuid | null",
  "campaign_id": "uuid | null",
  "timestamp": "2026-09-27T12:00:00.123Z",
  "payload": { }
}
```

## Catalogue

| Événement | Payload principal |
|---|---|
| `call.started` | `direction, status, from_number, to_number, contact_id` |
| `call.ringing` | `status` |
| `call.answered` | `direction, agent, status:"active"` |
| `call.transcript_partial` | `role:"user", text` (fragment de transcription en cours) |
| `call.transcript_final` | `role:"user", text` (tour utilisateur complet) |
| `call.response_generated` | `role:"assistant", text, interrupted` |
| `call.tts_started` | — (l'IA commence à parler) |
| `call.user_interrupted` | — (barge-in : l'IA s'est tue) |
| `call.tool_called` | `tool, args, allowed, duration_ms, result_preview, rag_chunks` |
| `call.agent_decision` | `agent, decision, reason, confidence, data` (route, steer, transfer, hallucination_suspected…) |
| `call.latency` | `voice_to_voice_ms` |
| `call.updated` | `status` (ex. `transferring`) |
| `call.transferred` / `call.completed` / `call.failed` | `status, outcome, intent, sentiment, summary, duration_seconds, cost_estimate, objections, error` |
| `campaign.status` | `status` (`running`, `paused`, `stopped`, `completed`, `failed`) |
| `campaign.progress` | `status, total_contacts, pending, calling, completed, failed, opted_out, retry, active_calls, success_count, percent` |
| `analytics.updated` | `active_calls, active_by_direction, calls_today, completed_today, failed_today, cost_today, avg_latency_ms, capacity, utilization` |

Les noms d'événements sont stables : un ajout de champ n'est pas un breaking change, un renommage l'est.
