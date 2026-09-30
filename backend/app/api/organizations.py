from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import DB, CurrentPrincipal
from app.models import Organization
from app.core.config import get_settings
from app.core.languages import FISH_FR_VOICES, LANGUAGES, RECOMMENDED_FR, VOICES
from app.schemas import OrganizationOut, OrganizationUpdate, VoicesOut

router = APIRouter(prefix="/organizations", tags=["organizations"])


@router.get("/me", response_model=OrganizationOut)
async def get_my_org(p: CurrentPrincipal) -> Organization:
    return p.organization


@router.patch("/me", response_model=OrganizationOut)
async def update_my_org(body: OrganizationUpdate, p: CurrentPrincipal, db: DB) -> Organization:
    org = await db.merge(p.organization)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(org, k, v)
    await db.commit()
    return org


@router.get("/voices", response_model=VoicesOut, tags=["voices"])
async def list_voices(p: CurrentPrincipal) -> dict:
    """Voix Gemini Live disponibles (pour le sélecteur du frontend) et langues supportées."""
    s = get_settings()
    if s.live_provider == "cascade":
        return {
            "default_voice": p.organization.default_voice_id or s.fish_voice_id or next(iter(FISH_FR_VOICES)),
            "default_language": p.organization.default_language or s.default_language,
            "languages": [{"code": l.code, "locale": l.locale, "name": l.name} for l in LANGUAGES.values()],
            "voices": [{"name": vid, "style": f"{n} — {st}", "recommended_fr": True} for vid, (n, st) in FISH_FR_VOICES.items()],
        }
    order = RECOMMENDED_FR + [v for v in VOICES if v not in RECOMMENDED_FR]
    return {
        "default_voice": p.organization.default_voice_id or s.gemini_default_voice,
        "default_language": p.organization.default_language or s.default_language,
        "languages": [{"code": l.code, "locale": l.locale, "name": l.name} for l in LANGUAGES.values()],
        "voices": [{"name": v, "style": VOICES[v], "recommended_fr": v in RECOMMENDED_FR} for v in order],
    }
