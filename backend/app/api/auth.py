from __future__ import annotations

import re
import uuid

import jwt
from fastapi import APIRouter, Request
from sqlalchemy import select

from app.api.deps import DB, APIError, CurrentPrincipal
from app.core.config import get_settings
from app.core.security import SlidingWindowRateLimiter, create_token, decode_token, hash_password, verify_password
from app.models import Organization, Role, User
from app.schemas import LoginIn, RefreshIn, RegisterIn, TokenOut, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])
_login_limiter = SlidingWindowRateLimiter(get_settings().login_rate_limit_per_minute)


def _tokens(user: User) -> TokenOut:
    return TokenOut(
        access_token=create_token(user.id, user.organization_id, "access"),
        refresh_token=create_token(user.id, user.organization_id, "refresh"),
        expires_in=get_settings().access_token_ttl_minutes * 60,
    )


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:80] or "org"


@router.post("/register", response_model=TokenOut, status_code=201)
async def register(body: RegisterIn, db: DB) -> TokenOut:
    email = body.email.lower()
    if await db.scalar(select(User).where(User.email == email)):
        raise APIError(409, "email_taken", "Un compte existe déjà avec cet email")
    org = Organization(name=body.organization_name, slug=f"{slugify(body.organization_name)}-{uuid.uuid4().hex[:6]}")
    db.add(org)
    await db.flush()
    user = User(email=email, password_hash=hash_password(body.password), full_name=body.full_name, role=Role.owner.value, organization_id=org.id)
    db.add(user)
    await db.commit()
    return _tokens(user)


@router.post("/login", response_model=TokenOut)
async def login(body: LoginIn, db: DB, request: Request) -> TokenOut:
    key = f"{request.client.host if request.client else 'x'}:{body.email.lower()}"
    if not _login_limiter.allow(key):
        raise APIError(429, "rate_limited", "Trop de tentatives, réessayez dans une minute")
    user = await db.scalar(select(User).where(User.email == body.email.lower()))
    if user is None or not verify_password(body.password, user.password_hash):
        raise APIError(401, "invalid_credentials", "Email ou mot de passe incorrect")
    return _tokens(user)


@router.post("/refresh", response_model=TokenOut)
async def refresh(body: RefreshIn, db: DB) -> TokenOut:
    try:
        payload = decode_token(body.refresh_token, "refresh")
    except jwt.InvalidTokenError:
        raise APIError(401, "invalid_token", "Refresh token invalide")
    user = await db.get(User, uuid.UUID(payload["sub"]))
    if user is None:
        raise APIError(401, "invalid_token", "Utilisateur inconnu")
    return _tokens(user)


@router.get("/me", response_model=UserOut)
async def me(p: CurrentPrincipal) -> User:
    return p.user
