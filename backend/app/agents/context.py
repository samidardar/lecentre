"""Contexte d'appel (config client figée au début de l'appel) et état runtime partagé entre graphes."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import Call, Campaign, Contact, Organization, PhoneNumber

Direction = Literal["inbound", "outbound"]


@dataclass
class CallContext:
    call_id: str
    organization_id: str
    direction: Direction
    org_name: str
    org_description: str
    tone: str
    language: str
    voice: str
    timezone: str
    from_number: str
    to_number: str
    campaign_id: str | None = None
    contact_id: str | None = None
    objective: str = ""
    objective_description: str = ""
    script: str = ""
    context: str = ""
    greeting: str | None = None
    knowledge_base_ids: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    transfer_number: str | None = None
    voicemail_behavior: str = "leave_message"
    voicemail_message: str | None = None
    webhook_url: str | None = None
    business_hours: dict[str, Any] = field(default_factory=dict)
    contact: dict[str, Any] = field(default_factory=dict)
    max_duration_s: int = 300

    @property
    def caller_number(self) -> str:
        return self.from_number if self.direction == "inbound" else self.to_number

    def is_open_now(self) -> bool:
        """business_hours: {"mon": ["09:00","18:00"], ...} ; vide = toujours ouvert."""
        if not self.business_hours:
            return True
        now = datetime.now(ZoneInfo(self.timezone))
        key = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][now.weekday()]
        span = self.business_hours.get(key)
        if not span:
            return False
        return span[0] <= now.strftime("%H:%M") < span[1]


@dataclass
class CallRuntime:
    """État mutable d'un appel, partagé par la session, les outils et le Supervisor."""

    ctx: CallContext
    started_monotonic: float = field(default_factory=time.monotonic)
    transcript: list[dict[str, Any]] = field(default_factory=list)
    rag_hits: list[dict[str, Any]] = field(default_factory=list)
    outcome: dict[str, Any] = field(default_factory=dict)
    pending_action: dict[str, Any] | None = None
    slots: dict[str, Any] = field(default_factory=dict)
    messages: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: int = 0
    turn_count: int = 0
    strikes: dict[str, int] = field(default_factory=dict)
    usage: dict[str, int] = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0})
    voicemail: bool = False

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_monotonic

    def add_turn(self, role: str, text: str, **extra: Any) -> None:
        self.transcript.append({"role": role, "text": text, "at": round(self.elapsed, 2), **extra})


async def load_call_context(db: AsyncSession, call_id: uuid.UUID) -> CallContext:
    call = await db.get(Call, call_id)
    if call is None:
        raise LookupError(f"call {call_id} introuvable")
    org = await db.get(Organization, call.organization_id)
    assert org is not None
    s = get_settings()
    ctx = CallContext(
        call_id=str(call.id), organization_id=str(org.id), direction=call.direction,  # type: ignore[arg-type]
        org_name=org.name, org_description=org.business_description, tone=org.tone, language=org.default_language,
        voice=org.default_voice_id or s.gemini_default_voice, timezone=org.timezone,
        from_number=call.from_number, to_number=call.to_number, transfer_number=org.transfer_number,
        max_duration_s=int((org.settings or {}).get("max_call_seconds", s.default_max_call_seconds)),
    )
    if call.campaign_id:
        camp = await db.get(Campaign, call.campaign_id)
        if camp is not None:
            ctx.campaign_id = str(camp.id)
            ctx.objective, ctx.objective_description = camp.objective, camp.objective_description
            ctx.script, ctx.context, ctx.language = camp.script, camp.context, camp.language
            ctx.voice = camp.voice_id or ctx.voice
            ctx.allowed_tools = list(camp.allowed_tools or [])
            ctx.transfer_number = camp.transfer_number or ctx.transfer_number
            ctx.voicemail_behavior, ctx.voicemail_message = camp.voicemail_behavior, camp.voicemail_message
            ctx.webhook_url = camp.webhook_url
            if camp.knowledge_base_id:
                ctx.knowledge_base_ids = [str(camp.knowledge_base_id)]
    if call.phone_number_id:
        pn = await db.get(PhoneNumber, call.phone_number_id)
        if pn is not None:
            ctx.greeting, ctx.script = pn.greeting, pn.script or ctx.script
            ctx.voice = pn.voice_id or ctx.voice
            ctx.allowed_tools = list(pn.allowed_tools or ctx.allowed_tools)
            ctx.business_hours = pn.business_hours or {}
            ctx.transfer_number = pn.transfer_number or ctx.transfer_number
            if pn.knowledge_base_id:
                ctx.knowledge_base_ids = [str(pn.knowledge_base_id)]
    if not ctx.allowed_tools:
        ctx.allowed_tools = ["search_knowledge_base", "take_message", "transfer_to_human"]
    if call.contact_id:
        contact = await db.get(Contact, call.contact_id)
        if contact is not None:
            ctx.contact_id = str(contact.id)
            ctx.contact = {"first_name": contact.first_name, "last_name": contact.last_name, "phone": contact.phone,
                           "email": contact.email, **(contact.attributes or {})}
    return ctx
