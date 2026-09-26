"""Contrats API (Pydantic v2). Toute modification ici = modification du contrat frontend."""
from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.core.languages import LANGUAGES, validate_voice

T = TypeVar("T")
E164 = re.compile(r"^\+[1-9]\d{6,14}$")


def normalize_phone(value: str, default_cc: str = "+33") -> str:
    v = re.sub(r"[\s.\-()]", "", value or "")
    if v.startswith("00"):
        v = "+" + v[2:]
    elif v.startswith("0") and len(v) == 10:
        v = default_cc + v[1:]
    if not E164.match(v):
        raise ValueError(f"numéro invalide (format E.164 attendu): {value!r}")
    return v


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class ErrorBody(BaseModel):
    code: str
    message: str
    details: Any | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


# ---------- Auth ----------
class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str | None = None
    organization_name: str = Field(min_length=2, max_length=200)


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class RefreshIn(BaseModel):
    refresh_token: str


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


class UserOut(ORM):
    id: uuid.UUID
    email: str
    full_name: str | None
    role: str
    organization_id: uuid.UUID


# ---------- Organization ----------
class OrganizationOut(ORM):
    id: uuid.UUID
    name: str
    slug: str
    default_language: str
    default_voice_id: str
    timezone: str
    tone: str
    business_description: str
    transfer_number: str | None
    settings: dict[str, Any]
    created_at: datetime


def _check_language(v: str | None) -> str | None:
    if v is not None and v.split("-")[0].lower() not in LANGUAGES:
        raise ValueError(f"langue non supportée: {v!r} (supportées: {', '.join(LANGUAGES)})")
    return v


class OrganizationUpdate(BaseModel):
    name: str | None = None
    default_language: str | None = None
    default_voice_id: str | None = None

    _v_voice = field_validator("default_voice_id")(classmethod(lambda cls, v: validate_voice(v)))
    _v_lang = field_validator("default_language")(classmethod(lambda cls, v: _check_language(v)))
    timezone: str | None = None
    tone: str | None = None
    business_description: str | None = None
    transfer_number: str | None = None
    settings: dict[str, Any] | None = None


# ---------- Campaigns ----------
Objective = Literal[
    "confirm_appointment", "qualify_lead", "request_payment", "collect_feedback",
    "invite_event", "reactivate_customer", "schedule_meeting", "custom",
]


class CampaignSchedule(BaseModel):
    days: list[int] = Field(default=[0, 1, 2, 3, 4], description="0=lundi … 6=dimanche")
    start: str = "09:00"
    end: str = "19:00"


class CampaignBase(BaseModel):
    _v_voice = field_validator("voice_id", check_fields=False)(classmethod(lambda cls, v: validate_voice(v)))
    _v_lang = field_validator("language", check_fields=False)(classmethod(lambda cls, v: _check_language(v)))
    name: str = Field(min_length=1, max_length=200)
    objective: Objective = "qualify_lead"
    objective_description: str = ""
    script: str = ""
    context: str = ""
    voice_id: str | None = None
    language: str = "fr"
    max_concurrency: int = Field(default=10, ge=1, le=100)
    max_attempts: int = Field(default=2, ge=1, le=10)
    retry_delay_minutes: int = Field(default=60, ge=0)
    schedule: CampaignSchedule = Field(default_factory=CampaignSchedule)
    voicemail_behavior: Literal["leave_message", "hangup"] = "leave_message"
    voicemail_message: str | None = None
    knowledge_base_id: uuid.UUID | None = None
    from_number: str | None = None
    webhook_url: str | None = None
    allowed_tools: list[str] = Field(default_factory=lambda: ["search_knowledge_base", "book_appointment", "transfer_to_human"])
    transfer_number: str | None = None


class CampaignCreate(CampaignBase):
    pass


class CampaignUpdate(BaseModel):
    _v_voice = field_validator("voice_id", check_fields=False)(classmethod(lambda cls, v: validate_voice(v)))
    _v_lang = field_validator("language", check_fields=False)(classmethod(lambda cls, v: _check_language(v)))
    name: str | None = None
    objective: Objective | None = None
    objective_description: str | None = None
    script: str | None = None
    context: str | None = None
    voice_id: str | None = None
    language: str | None = None
    max_concurrency: int | None = Field(default=None, ge=1, le=100)
    max_attempts: int | None = Field(default=None, ge=1, le=10)
    retry_delay_minutes: int | None = None
    schedule: CampaignSchedule | None = None
    voicemail_behavior: Literal["leave_message", "hangup"] | None = None
    voicemail_message: str | None = None
    knowledge_base_id: uuid.UUID | None = None
    from_number: str | None = None
    webhook_url: str | None = None
    allowed_tools: list[str] | None = None
    transfer_number: str | None = None


class CampaignOut(ORM, CampaignBase):
    id: uuid.UUID
    organization_id: uuid.UUID
    status: str
    schedule: CampaignSchedule | dict[str, Any]  # type: ignore[assignment]
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class CampaignProgress(BaseModel):
    campaign_id: uuid.UUID
    status: str
    total_contacts: int
    pending: int
    calling: int
    completed: int
    failed: int
    opted_out: int
    retry: int
    active_calls: int
    success_count: int
    percent: float


# ---------- Contacts ----------
class ContactCreate(BaseModel):
    phone: str
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: str) -> str:
        return normalize_phone(v)


class ContactOut(ORM):
    id: uuid.UUID
    campaign_id: uuid.UUID | None
    first_name: str | None
    last_name: str | None
    phone: str
    email: str | None
    attributes: dict[str, Any]
    status: str
    attempts: int
    last_outcome: str | None
    created_at: datetime


class ContactImportResult(BaseModel):
    imported: int
    skipped: int
    errors: list[dict[str, Any]]
    detected_columns: list[str]
    mapping_used: dict[str, str]


# ---------- Phone numbers ----------
class PhoneNumberBase(BaseModel):
    _v_voice = field_validator("voice_id", check_fields=False)(classmethod(lambda cls, v: validate_voice(v)))
    number: str
    provider: Literal["twilio", "mock"] = "twilio"
    direction: Literal["inbound", "outbound", "both"] = "both"
    active: bool = True
    label: str | None = None
    greeting: str | None = None
    script: str | None = None
    knowledge_base_id: uuid.UUID | None = None
    voice_id: str | None = None
    business_hours: dict[str, Any] = Field(default_factory=dict)
    allowed_tools: list[str] = Field(default_factory=lambda: ["search_knowledge_base", "take_message", "transfer_to_human", "book_appointment"])
    transfer_number: str | None = None

    @field_validator("number")
    @classmethod
    def _num(cls, v: str) -> str:
        return normalize_phone(v)


class PhoneNumberCreate(PhoneNumberBase):
    pass


class PhoneNumberUpdate(BaseModel):
    _v_voice = field_validator("voice_id", check_fields=False)(classmethod(lambda cls, v: validate_voice(v)))
    active: bool | None = None
    label: str | None = None
    direction: Literal["inbound", "outbound", "both"] | None = None
    greeting: str | None = None
    script: str | None = None
    knowledge_base_id: uuid.UUID | None = None
    voice_id: str | None = None
    business_hours: dict[str, Any] | None = None
    allowed_tools: list[str] | None = None
    transfer_number: str | None = None


class PhoneNumberOut(ORM, PhoneNumberBase):
    id: uuid.UUID
    organization_id: uuid.UUID
    created_at: datetime

    @field_validator("number")
    @classmethod
    def _num(cls, v: str) -> str:  # pas de re-validation en sortie
        return v


class VoiceOut(BaseModel):
    name: str
    style: str
    recommended_fr: bool


class VoicesOut(BaseModel):
    default_voice: str
    default_language: str
    languages: list[dict[str, str]]
    voices: list[VoiceOut]


# ---------- Calls ----------
class TranscriptTurn(BaseModel):
    role: Literal["user", "assistant", "system", "tool"]
    text: str
    at: float = Field(description="secondes depuis le début de l'appel")
    interrupted: bool = False


class CallOut(ORM):
    id: uuid.UUID
    organization_id: uuid.UUID
    campaign_id: uuid.UUID | None
    contact_id: uuid.UUID | None
    direction: str
    status: str
    provider: str
    from_number: str
    to_number: str
    started_at: datetime
    answered_at: datetime | None
    ended_at: datetime | None
    duration_seconds: int
    cost_estimate: float
    outcome: str | None
    intent: str | None
    sentiment: float | None
    summary: str | None
    created_at: datetime


class CallDetail(CallOut):
    transcript: list[dict[str, Any]] | None
    latency: dict[str, Any]
    cost_breakdown: dict[str, Any]
    next_best_action: str | None
    error: str | None


class CallEventOut(ORM):
    id: uuid.UUID
    type: str
    payload: dict[str, Any]
    created_at: datetime


class AgentDecisionOut(ORM):
    id: uuid.UUID
    agent: str
    decision: str
    confidence: float
    reason: str
    data: dict[str, Any]
    created_at: datetime


class CallDebug(BaseModel):
    call: CallDetail
    events: list[CallEventOut]
    decisions: list[AgentDecisionOut]
    rag_chunks: list[dict[str, Any]]


class SimulateCallIn(BaseModel):
    direction: Literal["inbound", "outbound"] = "inbound"
    campaign_id: uuid.UUID | None = None
    phone_number_id: uuid.UUID | None = None
    caller_script: list[str] = Field(
        default_factory=lambda: ["Bonjour, quels sont vos horaires d'ouverture ?", "Merci, au revoir."],
        description="Répliques de l'appelant simulé (mode mock)",
    )


class OutboundTestIn(BaseModel):
    to: str
    campaign_id: uuid.UUID | None = None

    @field_validator("to")
    @classmethod
    def _to(cls, v: str) -> str:
        return normalize_phone(v)


# ---------- Knowledge ----------
class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""


class DocumentOut(ORM):
    id: uuid.UUID
    knowledge_base_id: uuid.UUID
    filename: str
    source_type: str
    status: str
    error: str | None
    chunk_count: int
    created_at: datetime


class KnowledgeBaseOut(ORM):
    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    description: str
    created_at: datetime


class KnowledgeBaseDetail(KnowledgeBaseOut):
    documents: list[DocumentOut]


class TextDocumentIn(BaseModel):
    filename: str = "note.md"
    content: str = Field(min_length=1)


class TestQueryIn(BaseModel):
    query: str = Field(min_length=1)
    top_k: int = Field(default=4, ge=1, le=20)


class RetrievedChunk(BaseModel):
    chunk_id: str
    document_id: str
    content: str
    score: float
    source: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class TestQueryOut(BaseModel):
    query: str
    chunks: list[RetrievedChunk]
    low_confidence: bool
    latency_ms: float


# ---------- Analytics ----------
class AnalyticsOverview(BaseModel):
    period_days: int
    total_calls: int
    completed_calls: int
    failed_calls: int
    transferred_calls: int
    active_calls: int
    queued_calls: int
    average_duration_s: float
    average_cost: float
    total_cost: float
    average_sentiment: float | None
    conversion_rate: float
    transfer_rate: float
    average_voice_latency_ms: float | None
    p95_voice_latency_ms: float | None
    top_intents: list[dict[str, Any]]
    outcomes: dict[str, int]
    calls_per_hour: list[dict[str, Any]]
    running_campaigns: int


class RealtimeSnapshot(BaseModel):
    active_calls: int
    active_by_direction: dict[str, int]
    calls_today: int
    completed_today: int
    failed_today: int
    cost_today: float
    avg_latency_ms: float | None
    capacity: int
    utilization: float


class CampaignAnalytics(BaseModel):
    campaign_id: uuid.UUID
    contacts: int
    calls_made: int
    human_answers: int
    voicemails: int
    successes: int
    success_rate: float
    total_cost: float
    cost_per_success: float | None
    average_duration_s: float
    outcomes: dict[str, int]
    top_objections: list[dict[str, Any]]
    failure_reasons: dict[str, int]
    recommendations: list[str]
