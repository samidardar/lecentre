from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import DB, CurrentPrincipal
from app.models import Organization
from app.schemas import OrganizationOut, OrganizationUpdate

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
