from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, File, Form, UploadFile
from sqlalchemy import select

from app.api.deps import DB, APIError, CurrentPrincipal, Paging, get_owned, paginate
from app.models import Campaign, CampaignStatus, Contact
from app.schemas import (
    CampaignCreate, CampaignOut, CampaignProgress, CampaignUpdate, ContactCreate, ContactImportResult, ContactOut, Page,
)
from app.services.contact_import import import_contacts
from app.services.dispatcher import campaign_progress, get_dispatcher

router = APIRouter(tags=["campaigns"])
EDITABLE_WHILE_RUNNING = {"max_concurrency", "schedule", "webhook_url", "name"}


@router.get("/campaigns", response_model=Page[CampaignOut])
async def list_campaigns(p: CurrentPrincipal, db: DB, paging: Paging, status: str | None = None) -> dict:
    stmt = select(Campaign).where(Campaign.organization_id == p.org_id).order_by(Campaign.created_at.desc())
    if status:
        stmt = stmt.where(Campaign.status == status)
    items, total = await paginate(db, stmt, paging)
    return {"items": items, "total": total, "limit": paging.limit, "offset": paging.offset}


@router.post("/campaigns", response_model=CampaignOut, status_code=201)
async def create_campaign(body: CampaignCreate, p: CurrentPrincipal, db: DB) -> Campaign:
    data = body.model_dump(mode="json")
    data["knowledge_base_id"] = body.knowledge_base_id
    camp = Campaign(organization_id=p.org_id, **data)
    db.add(camp)
    await db.commit()
    return camp


@router.get("/campaigns/{campaign_id}", response_model=CampaignOut)
async def get_campaign(campaign_id: uuid.UUID, p: CurrentPrincipal, db: DB) -> Campaign:
    return await get_owned(db, Campaign, campaign_id, p.org_id, "Campagne")


@router.patch("/campaigns/{campaign_id}", response_model=CampaignOut)
async def update_campaign(campaign_id: uuid.UUID, body: CampaignUpdate, p: CurrentPrincipal, db: DB) -> Campaign:
    camp = await get_owned(db, Campaign, campaign_id, p.org_id, "Campagne")
    changes = body.model_dump(exclude_unset=True, mode="json")
    if camp.status == CampaignStatus.running.value and set(changes) - EDITABLE_WHILE_RUNNING:
        raise APIError(409, "campaign_running", "Mettez la campagne en pause pour modifier ces champs",
                       details=sorted(set(changes) - EDITABLE_WHILE_RUNNING))
    if "knowledge_base_id" in changes:
        changes["knowledge_base_id"] = body.knowledge_base_id
    for k, v in changes.items():
        setattr(camp, k, v)
    await db.commit()
    return camp


@router.delete("/campaigns/{campaign_id}", status_code=204)
async def delete_campaign(campaign_id: uuid.UUID, p: CurrentPrincipal, db: DB) -> None:
    camp = await get_owned(db, Campaign, campaign_id, p.org_id, "Campagne")
    if camp.status == CampaignStatus.running.value:
        raise APIError(409, "campaign_running", "Arrêtez la campagne avant de la supprimer")
    await db.delete(camp)
    await db.commit()


@router.post("/campaigns/{campaign_id}/start", response_model=CampaignOut)
async def start_campaign(campaign_id: uuid.UUID, p: CurrentPrincipal, db: DB) -> Campaign:
    camp = await get_owned(db, Campaign, campaign_id, p.org_id, "Campagne")
    if camp.status == CampaignStatus.running.value:
        return camp
    has_contacts = await db.scalar(select(Contact.id).where(Contact.campaign_id == campaign_id).limit(1))
    if not has_contacts:
        raise APIError(422, "no_contacts", "Importez des contacts avant de lancer la campagne")
    await get_dispatcher().start(campaign_id)
    await db.refresh(camp)
    return camp


@router.post("/campaigns/{campaign_id}/pause", response_model=CampaignOut)
async def pause_campaign(campaign_id: uuid.UUID, p: CurrentPrincipal, db: DB) -> Campaign:
    camp = await get_owned(db, Campaign, campaign_id, p.org_id, "Campagne")
    if camp.status != CampaignStatus.running.value:
        raise APIError(409, "invalid_state", f"Campagne non active (statut: {camp.status})")
    await get_dispatcher().set_status(campaign_id, CampaignStatus.paused)
    await db.refresh(camp)
    return camp


@router.post("/campaigns/{campaign_id}/stop", response_model=CampaignOut)
async def stop_campaign(campaign_id: uuid.UUID, p: CurrentPrincipal, db: DB) -> Campaign:
    camp = await get_owned(db, Campaign, campaign_id, p.org_id, "Campagne")
    await get_dispatcher().set_status(campaign_id, CampaignStatus.stopped)
    await db.refresh(camp)
    return camp


@router.get("/campaigns/{campaign_id}/progress", response_model=CampaignProgress)
async def get_progress(campaign_id: uuid.UUID, p: CurrentPrincipal, db: DB) -> dict:
    await get_owned(db, Campaign, campaign_id, p.org_id, "Campagne")
    return await campaign_progress(campaign_id, get_dispatcher().active_calls(campaign_id))


# ------------------------------------------------------------------ contacts
@router.get("/campaigns/{campaign_id}/contacts", response_model=Page[ContactOut], tags=["contacts"])
async def list_contacts(campaign_id: uuid.UUID, p: CurrentPrincipal, db: DB, paging: Paging, status: str | None = None) -> dict:
    await get_owned(db, Campaign, campaign_id, p.org_id, "Campagne")
    stmt = select(Contact).where(Contact.campaign_id == campaign_id).order_by(Contact.created_at)
    if status:
        stmt = stmt.where(Contact.status == status)
    items, total = await paginate(db, stmt, paging)
    return {"items": items, "total": total, "limit": paging.limit, "offset": paging.offset}


@router.post("/campaigns/{campaign_id}/contacts", response_model=list[ContactOut], status_code=201, tags=["contacts"])
async def add_contacts(campaign_id: uuid.UUID, body: list[ContactCreate], p: CurrentPrincipal, db: DB) -> list[Contact]:
    await get_owned(db, Campaign, campaign_id, p.org_id, "Campagne")
    contacts = [Contact(organization_id=p.org_id, campaign_id=campaign_id, **c.model_dump()) for c in body]
    db.add_all(contacts)
    await db.commit()
    return contacts


@router.post("/campaigns/{campaign_id}/contacts/import", response_model=ContactImportResult, tags=["contacts"])
async def import_csv(campaign_id: uuid.UUID, p: CurrentPrincipal, db: DB, file: UploadFile = File(...),
                     mapping: str | None = Form(None, description='JSON optionnel, ex. {"phone": "Mobile", "first_name": "Prénom"}')) -> dict:
    await get_owned(db, Campaign, campaign_id, p.org_id, "Campagne")
    try:
        mapping_dict = json.loads(mapping) if mapping else None
    except json.JSONDecodeError:
        raise APIError(422, "invalid_mapping", "mapping doit être un objet JSON")
    data = await file.read()
    if len(data) > 20 * 1024 * 1024:
        raise APIError(413, "file_too_large", "Fichier limité à 20 Mo")
    return await import_contacts(db, organization_id=p.org_id, campaign_id=campaign_id, data=data, mapping=mapping_dict)


@router.delete("/contacts/{contact_id}", status_code=204, tags=["contacts"])
async def delete_contact(contact_id: uuid.UUID, p: CurrentPrincipal, db: DB) -> None:
    """Suppression RGPD : le contact et les transcripts de ses appels sont effacés."""
    from sqlalchemy import update

    from app.models import Call

    contact = await get_owned(db, Contact, contact_id, p.org_id, "Contact")
    await db.execute(update(Call).where(Call.contact_id == contact_id).values(transcript=None, summary=None, to_number="[supprimé]"))
    await db.delete(contact)
    await db.commit()
