# Temps de réponse et qualité de voix — mesures et réglages

Mesures réelles du 06/11/2026 (Fish Audio `s2.1-pro-free`, Deepgram Nova-3, depuis un poste en France).
Le temps de réponse du LLM (OpenRouter) n'est pas encore mesuré : `callwiz doctor --live` le donne dès que la clé est en place.

## D'où vient le délai quand l'IA répond

| Étape | Mesure | Ce qui a été fait |
|---|---|---|
| Détection de la fin de parole (Deepgram) | ≈ 0,58 s après la dernière syllabe | `endpointing=300` ; `no_delay` testé, sans gain (595 ms) |
| Coupure sur une pause courte (« Bonjour, … ») | — | +600 ms d'attente si l'énoncé fait ≤ 2 mots, annulée si l'appelant reprend |
| Recherche dans la base | 13–18 ms | faite **avant** l'appel au LLM, extraits joints au message : plus de second aller-retour LLM pour les questions FAQ |
| Connexion au LLM | 100–300 ms de TLS évités | connexions gardées 2 min (au lieu de 5 s par défaut) et ouvertes dès le décroché |
| Premier mot du LLM | à mesurer (`doctor --live`) | modèle rapide `anthropic/claude-haiku-5.5`, routage OpenRouter « latency » |
| Premier son de la voix (Fish) | 0,75–1,07 s en `latency=low`/`balanced` ; **3,2–3,5 s en `normal`** | `FISH_LATENCY=low` ; connexion voix préouverte entre deux tours |
| Taille du premier morceau envoyé à la voix | sans effet (≈ 0,75 s quelle que soit la longueur) | on garde des phrases entières (meilleure intonation) |
| Accueil | 1er appel jamais vu : ≈ 1,5 s ; **ensuite : 0–2 ms** | audio en cache disque, préchauffé au démarrage du serveur et à chaque modification d'un numéro |
| Appel d'un outil (RDV, recherche…) | un aller-retour LLM de plus | « Je regarde ça. » joué instantanément depuis le cache pendant l'outil |

Chaque tour enregistre sa décomposition (`IA : 1er mot`, `voix : 1er son`, `total jusqu'au son`, `source` llm/cache),
visible en direct dans la console (« Mesures ») et dans le détail de chaque appel (médianes par étape).

## Qualité de la voix

**Prononciation.** Aller-retour réel voix → transcription :
- texte brut « Ouvert de 9h à 19h. La révision coûte 89€. Rappelez le 06 12 34 56 78 pour votre RDV. » :
  la dernière phrase (numéro de téléphone, rendez-vous) est perdue ;
- même texte normalisé : tout est compris.

Le normaliseur (`app/voice/tts_text.py`) écrit en toutes lettres les heures (« 9h30-12h » → « neuf heures trente à
midi »), les prix (« 29,90 € » → « vingt-neuf euros quatre-vingt-dix »), les numéros de téléphone (par paires), les dates,
les pourcentages, les nombres et les abréviations (RDV, TTC, SAV, M., Mme…), et retire markdown, émojis et URL. La
transcription affichée garde le texte d'origine.

**Débit.** Cible : ~170 mots/min, le débit naturel au téléphone. Mesures à vitesse 1.0 → réglage appliqué par voix
(`FISH_VOICE_SPEED` dans `app/core/languages.py`) :

| Voix | À vitesse 1.0 | Vitesse réglée | Résultat |
|---|---|---|---|
| Féminine | 171 mots/min | 1.0 | 171 |
| Clémence | 168 | 1.0 | 168 |
| Manon | 180 (200 sur phrase courte) | 0.95 | ~170 |
| Narratrice claire | 198 | 0.85 | ~168 |
| Stoïc | 172 | 1.0 | 172 |
| Lucas | 195 | 0.9 | 171 |

`FISH_SPEED` (défaut 1.0) multiplie ce réglage pour toute l'organisation.

## Pour aller plus loin
- Modèle Fish payant (`FISH_MODEL=s2.1-pro`) : qualité supérieure ; latence à re-mesurer avec `callwiz doctor --live`.
- Hébergement en Europe (proche de Fish, Deepgram, OpenRouter) : moins de latence réseau qu'un PC derrière un tunnel.
- Mesurer la latence voix-à-voix réelle sur 20 appels et ajuster `DEEPGRAM_ENDPOINTING_MS` (300 → 250 si les coupures
  restent rares).
