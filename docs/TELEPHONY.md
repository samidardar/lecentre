# Télécom — Twilio

## Prérequis
1. Compte Twilio, un numéro voix (FR : justificatifs réglementaires requis).
2. URL publique HTTPS vers l'API : `cloudflared tunnel --url http://localhost:8000` ou `ngrok http 8000`.
3. `.env` :
   ```
   TELEPHONY_PROVIDER=twilio
   TWILIO_ACCOUNT_SID=AC…
   TWILIO_AUTH_TOKEN=…
   TWILIO_DEFAULT_FROM=+33…
   PUBLIC_BASE_URL=https://<votre-tunnel>
   LIVE_PROVIDER=gemini
   GEMINI_API_KEY=…
   ```

## Entrant
Console Twilio → numéro → *A call comes in* : Webhook `POST https://<public>/telephony/twilio/voice`.
Déclarer le numéro côté CallWiz : `POST /api/v1/phone-numbers` (greeting, KB, horaires, numéro de transfert).
Flux : webhook → création `Call` → TwiML `<Connect><Stream url="wss://…/telephony/twilio/media">` → session Gemini Live.

## Sortant
`POST /api/v1/campaigns/{id}/start` ou `POST /api/v1/calls/test-outbound {"to": "+336…"}`.
L'appel est créé avec `MachineDetection=DetectMessageEnd` + `AsyncAmd` : si un répondeur est détecté, l'agent laisse le message configuré (`voicemail_behavior=leave_message`) ou raccroche (`hangup`).

## Sécurité
Toutes les requêtes Twilio sont vérifiées (`X-Twilio-Signature`, HMAC-SHA1 sur `PUBLIC_BASE_URL` + chemin + paramètres). `PUBLIC_BASE_URL` doit donc être exactement l'URL configurée chez Twilio.

## Transfert / raccrochage
L'agent appelle `transfer_to_human` → à la fin de sa phrase, CallWiz met à jour l'appel Twilio avec `<Dial>` vers `transfer_number` (campagne, numéro ou organisation). Hors horaires (`business_hours`), le transfert est refusé et l'agent prend un message.

## Audio
Twilio : µ-law 8 kHz, frames 20 ms. CallWiz convertit en PCM16 16 kHz (entrée Gemini, envoi par paquets de 40 ms) et reconvertit la sortie Gemini PCM16 24 kHz → µ-law 8 kHz. Barge-in : `interrupted` Gemini → message `clear` Twilio (vide le buffer de lecture instantanément).

## Autres providers
Implémenter `TelephonyProvider` (`app/telephony/base.py`) et un `MediaTransport` (`app/voice/transport.py`). Telnyx (Media Streaming WS) et FreeSWITCH (`mod_audio_stream`) suivent le même schéma.

## Conformité France
Annonce IA dès la première phrase (imposée par les instructions et vérifiée par le Supervisor), opt-out reconnu et persisté (`opted_out` : le contact n'est plus appelé), fenêtres horaires par campagne, consultation Bloctel à faire en amont de l'import pour la prospection B2C.
