# Configuration : APIs, `.env`, français et voix

> **Pipeline par défaut depuis le 30/09/2026 : « cascade »** — Deepgram Nova-3 (transcription streaming, français)
> → Claude Haiku 4.5 (conversation, outils) → Fish Audio (voix, WebSocket streaming). `LIVE_PROVIDER=cascade`.
> Gemini Live reste disponible en option (`LIVE_PROVIDER=gemini`), décrit plus bas.

## 0. Clés du pipeline cascade

| Service | Usage | Où | Variables |
|---|---|---|---|
| **Anthropic (Claude)** | Conversation temps réel + analyse de fin d'appel | console.anthropic.com → API Keys | `ANTHROPIC_API_KEY` (+ `ANTHROPIC_WORKSPACE_ID` si la clé n'est rattachée à aucun workspace) |
| **Deepgram** | Transcription en streaming de l'appelant | console.deepgram.com → API Keys | `DEEPGRAM_API_KEY` |
| **Fish Audio** | Voix de l'agent | fish.audio/app/developers (crédit API séparé du crédit plateforme) | `FISH_API_KEY`, `FISH_VOICE_ID` |
| **Twilio** | Téléphonie | console.twilio.com | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_DEFAULT_FROM` |

Voix Fish Audio françaises présélectionnées (aucune n'imite une personne réelle) : `GET /api/v1/organizations/voices`
ou `backend/app/core/languages.py` (`FISH_FR_VOICES`). Une voix se règle par numéro, campagne ou organisation
(`voice_id` = reference_id Fish de 32 caractères) ou globalement (`FISH_VOICE_ID`).

Latence (mesurée) : ouverture WebSocket Fish ≈ 0,9 s (préouverte entre deux tours, donc invisible) ; premier son
≈ 0,8 s après le texte sur `s2.1-pro-free`. Le texte part vers la voix phrase par phrase, dès la première phrase.

---

## 1. APIs nécessaires

| Service | Obligatoire ? | Usage dans CallWiz | Où l'obtenir | Variables |
|---|---|---|---|---|
| **Google Gemini API** | Oui (en réel) | Voix temps réel (Gemini Live `gemini-3.8-live`) + analyse post-appel (`gemini-3.5-flash-lite`) + embeddings optionnels | [Google AI Studio](https://aistudio.google.com/apikey) → *Create API key* (activer la facturation pour lever les quotas gratuits) | `GEMINI_API_KEY` |
| **Twilio Programmable Voice** | Oui (vrais appels) | Numéros, appels entrants/sortants, Media Streams, détection de répondeur, transfert | [console.twilio.com](https://console.twilio.com) → Account SID + Auth Token, puis acheter un numéro FR (+33) : un *Regulatory Bundle* (justificatifs d'identité / d'adresse) est exigé pour la France | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_DEFAULT_FROM` |
| **Tunnel HTTPS public** | Oui en local | Twilio doit joindre vos webhooks et le WebSocket média | `cloudflared tunnel --url http://localhost:8000` (gratuit) ou `ngrok http 8000` | `PUBLIC_BASE_URL` |
| PostgreSQL | Prod | Base de données (SQLite suffit en local) | Supabase, Neon, RDS… ou `docker compose` | `DATABASE_URL` |
| Redis | Prod multi-instance | Bus d'événements entre API et worker | Upstash, Redis Cloud… ou `docker compose` | `REDIS_URL` |
| Qdrant | Optionnel | Vecteurs RAG (le mode embarqué local fonctionne sans serveur) | Qdrant Cloud ou `docker compose` | `QDRANT_URL`, `QDRANT_API_KEY` |

Aucune autre clé n'est requise : Deepgram, ElevenLabs, OpenAI ou Anthropic ne sont **pas** utilisés. Les embeddings `fastembed` sont locaux : le modèle (~220 Mo) est téléchargé une fois au premier démarrage.

## 2. Fichier `.env` minimal pour de vrais appels en français

Créez `backend/.env` (`cd backend && callwiz init` copie `.env.example`), puis renseignez :

```dotenv
# --- Sécurité ---
JWT_SECRET=<chaîne aléatoire ≥ 32 caractères, générée par callwiz init>
PUBLIC_BASE_URL=https://<votre-tunnel>.trycloudflare.com

# --- Gemini Live (voix + LLM) ---
LIVE_PROVIDER=gemini
GEMINI_API_KEY=<clé AI Studio>
GEMINI_LIVE_MODEL=gemini-3.8-live
GEMINI_TEXT_MODEL=gemini-3.5-flash-lite

# --- Français & voix ---
DEFAULT_LANGUAGE=fr
GEMINI_DEFAULT_VOICE=Sulafat
LIVE_LANGUAGE_LOCK=true

# --- Twilio ---
TELEPHONY_PROVIDER=twilio
TWILIO_ACCOUNT_SID=AC...
TWILIO_AUTH_TOKEN=...
TWILIO_DEFAULT_FROM=+33XXXXXXXXX
```

Le reste garde ses valeurs par défaut (SQLite, Qdrant embarqué, fastembed). Ne commitez jamais `.env` : il est dans `.gitignore`.

Côté Twilio : *Phone Numbers → votre numéro → Voice → A call comes in* : `Webhook POST https://<tunnel>/telephony/twilio/voice`. Enregistrez ensuite ce numéro dans CallWiz (`POST /api/v1/phone-numbers`) pour l'appel entrant.

## 3. Langue française

Les modèles Live en audio natif détectent la langue automatiquement. Google recommande de la **verrouiller par les instructions système**, et CallWiz le fait à chaque appel :

- français de France, vouvoiement, formules de politesse naturelles, pas d'anglicismes ;
- heures dites sur 24 h (« quatorze heures trente »), prix en euros en toutes lettres, numéros dictés par paires, alphabet français pour épeler ;
- `LIVE_LANGUAGE_LOCK=true` : l'agent reste en français même si l'appelant change de langue. Avec `false`, il suit la langue de l'appelant.

Niveaux de réglage (le plus spécifique l'emporte) :
- organisation : `PATCH /api/v1/organizations/me {"default_language": "fr"}` ;
- campagne : `"language": "fr"` ;
- défaut global : `DEFAULT_LANGUAGE`.

Langues supportées pour l'instant : `fr`, `en`. En ajouter une : un profil dans `backend/app/core/languages.py`.

`LIVE_SEND_LANGUAGE_CODE=true` envoie aussi `language_code=fr-FR` au modèle et comme indice de transcription. Laissez-le à `false` sauf si votre modèle l'accepte : la documentation indique que les modèles audio natifs gèrent la langue eux-mêmes.

## 4. Voix

Les 30 voix Gemini parlent toutes français. Pour un centre d'appels, écoutez-les dans AI Studio (Live / Stream) avant de choisir. Sélection conseillée :

| Voix | Style | Usage conseillé |
|---|---|---|
| **Sulafat** *(défaut)* | Chaleureuse | Standard, service client |
| **Achird** | Amicale | Prospection, relances |
| **Kore** | Ferme | Recouvrement, confirmations |
| **Charon** | Informative | Support, explications |
| **Aoede** | Légère | Marques grand public |
| **Schedar** | Posée | Santé, juridique |
| **Iapetus** | Claire | Messages répondeur |
| **Vindemiatrix** | Douce | Rappels sensibles |

Niveaux de réglage (le plus spécifique l'emporte) :
- numéro entrant : `voice_id` ;
- campagne : `voice_id` ;
- organisation : `default_voice_id` ;
- défaut global : `GEMINI_DEFAULT_VOICE`.

Le nom de voix est validé par l'API (erreur 422 si inconnu, casse ignorée). Le frontend récupère la liste via `GET /api/v1/organizations/voices` (voix, style, `recommended_fr`, langues).

`LIVE_AFFECTIVE_DIALOG=true` adapte le ton à l'émotion de l'appelant. Cette option passe par l'API `v1beta` et n'est pas supportée par tous les modèles Live : testez-la avant de l'activer en production.

## 5. Vérification

```bash
cd backend
callwiz dev
```

Dans un second terminal :

```bash
callwiz call test --to +336XXXXXXXX
```

Vous recevez l'appel en français avec la voix choisie. Pour en tester une autre, changez `default_voice_id` (ou `GEMINI_DEFAULT_VOICE`) et rappelez.
