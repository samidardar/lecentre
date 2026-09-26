from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated, Any, TypeVar

import jwt
from fastapi import Depends, Header, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import org_id_var
from app.core.security import decode_token
from app.db import get_session
from app.models import Base, Organization, User

DB = Annotated[AsyncSession, Depends(get_session)]
M = TypeVar("M", bound=Base)


class APIError(HTTPException):
    def __init__(self, status_code: int, code: str, message: str, details: Any = None) -> None:
        super().__init__(status_code=status_code, detail={"code": code, "message": message, "details": details})


def not_found(what: str) -> APIError:
    return APIError(status.HTTP_404_NOT_FOUND, "not_found", f"{what} introuvable")


@dataclass
class Principal:
    user: User
    organization: Organization

    @property
    def org_id(self) -> uuid.UUID:
        return self.organization.id


async def principal_from_token(token: str, db: AsyncSession) -> Principal:
    try:
        payload = decode_token(token, "access")
    except jwt.ExpiredSignatureError:
        raise APIError(401, "token_expired", "Token expiré")
    except jwt.InvalidTokenError:
        raise APIError(401, "invalid_token", "Token invalide")
    user = await db.get(User, uuid.UUID(payload["sub"]))
    if user is None or str(user.organization_id) != payload.get("org"):
        raise APIError(401, "invalid_token", "Utilisateur inconnu")
    org = await db.get(Organization, user.organization_id)
    assert org is not None
    org_id_var.set(str(org.id))
    return Principal(user=user, organization=org)


async def get_principal(db: DB, authorization: Annotated[str | None, Header()] = None) -> Principal:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise APIError(401, "not_authenticated", "Authentification requise")
    return await principal_from_token(authorization.split(" ", 1)[1].strip(), db)


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]


class Pagination:
    def __init__(self, limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)) -> None:
        self.limit = limit
        self.offset = offset


Paging = Annotated[Pagination, Depends()]


async def get_owned(db: AsyncSession, model: type[M], obj_id: uuid.UUID, org_id: uuid.UUID, what: str) -> M:
    """Chargement strictement scoppé par organisation (isolation multi-tenant)."""
    obj = await db.get(model, obj_id)
    if obj is None or getattr(obj, "organization_id", None) != org_id:
        raise not_found(what)
    return obj


async def paginate(db: AsyncSession, stmt: Any, paging: Pagination) -> tuple[list[Any], int]:
    total = await db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    rows = (await db.scalars(stmt.limit(paging.limit).offset(paging.offset))).all()
    return list(rows), int(total or 0)
