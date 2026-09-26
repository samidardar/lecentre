from __future__ import annotations

import uuid

from fastapi import APIRouter
from sqlalchemy import select

from app.api.deps import DB, APIError, CurrentPrincipal, get_owned
from app.models import PhoneNumber
from app.schemas import PhoneNumberCreate, PhoneNumberOut, PhoneNumberUpdate

router = APIRouter(prefix="/phone-numbers", tags=["phone-numbers"])


@router.get("", response_model=list[PhoneNumberOut])
async def list_numbers(p: CurrentPrincipal, db: DB) -> list[PhoneNumber]:
    return list((await db.scalars(select(PhoneNumber).where(PhoneNumber.organization_id == p.org_id).order_by(PhoneNumber.created_at))).all())


@router.post("", response_model=PhoneNumberOut, status_code=201)
async def create_number(body: PhoneNumberCreate, p: CurrentPrincipal, db: DB) -> PhoneNumber:
    if await db.scalar(select(PhoneNumber).where(PhoneNumber.number == body.number)):
        raise APIError(409, "number_taken", "Ce numéro est déjà enregistré")
    pn = PhoneNumber(organization_id=p.org_id, **body.model_dump())
    db.add(pn)
    await db.commit()
    return pn


@router.patch("/{number_id}", response_model=PhoneNumberOut)
async def update_number(number_id: uuid.UUID, body: PhoneNumberUpdate, p: CurrentPrincipal, db: DB) -> PhoneNumber:
    pn = await get_owned(db, PhoneNumber, number_id, p.org_id, "Numéro")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(pn, k, v)
    await db.commit()
    return pn


@router.delete("/{number_id}", status_code=204)
async def delete_number(number_id: uuid.UUID, p: CurrentPrincipal, db: DB) -> None:
    pn = await get_owned(db, PhoneNumber, number_id, p.org_id, "Numéro")
    await db.delete(pn)
    await db.commit()
