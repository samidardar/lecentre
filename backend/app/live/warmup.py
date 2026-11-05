"""Préchauffage du cache audio : l'accueil de chaque numéro entrant est synthétisé à l'avance,
pour que même le tout premier appel soit décroché avec une voix instantanée."""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import select

from app.agents.prompts import inbound_greeting
from app.core.config import Settings, get_settings
from app.core.languages import FISH_VOICE_ID
from app.db import session_factory
from app.live.fish import FishTTSStream
from app.live.tts_cache import get_tts_cache
from app.models import Organization, PhoneNumber
from app.voice.tts_text import normalize_for_tts

logger = logging.getLogger(__name__)


def _voice(settings: Settings, *candidates: str | None) -> str | None:
    for v in candidates:
        if v and FISH_VOICE_ID.match(v):
            return v
    return settings.fish_voice_id


async def warm_inbound_greetings(phone_number_id: uuid.UUID | None = None, settings: Settings | None = None) -> int:
    s = settings or get_settings()
    if s.live_provider != "cascade" or not s.fish_api_key:
        return 0
    cache = get_tts_cache(s)
    async with session_factory()() as db:
        stmt = select(PhoneNumber, Organization).join(Organization, Organization.id == PhoneNumber.organization_id).where(PhoneNumber.active.is_(True))
        if phone_number_id:
            stmt = stmt.where(PhoneNumber.id == phone_number_id)
        rows = (await db.execute(stmt)).all()
    warmed = 0
    for pn, org in rows:
        voice = _voice(s, pn.voice_id, org.default_voice_id)
        text = normalize_for_tts(inbound_greeting(org.name, pn.greeting))
        probe = FishTTSStream(s, voice)
        key = cache.key(text, probe.voice_id, probe.speed, probe.sample_rate)
        if cache.get(key) is not None:
            continue
        try:
            await cache.get_or_synth(key, text, lambda v=voice: FishTTSStream(s, v))
            warmed += 1
        except Exception as exc:
            logger.warning("préchauffage de l'accueil %s échoué: %s", pn.number, exc)
    if warmed:
        logger.info("accueils préchauffés", extra={"extra_fields": {"count": warmed}})
    return warmed
