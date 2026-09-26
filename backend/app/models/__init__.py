from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Index, Integer, String, Text, Uuid, Boolean
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


class IdMixin:
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Role(str, enum.Enum):
    owner = "owner"
    admin = "admin"
    member = "member"


class CampaignStatus(str, enum.Enum):
    draft = "draft"
    running = "running"
    paused = "paused"
    completed = "completed"
    stopped = "stopped"
    failed = "failed"


class ContactStatus(str, enum.Enum):
    pending = "pending"
    calling = "calling"
    completed = "completed"
    failed = "failed"
    retry = "retry"
    opted_out = "opted_out"


class CallStatus(str, enum.Enum):
    queued = "queued"
    ringing = "ringing"
    active = "active"
    completed = "completed"
    failed = "failed"
    transferred = "transferred"
    no_answer = "no_answer"
    busy = "busy"
    voicemail = "voicemail"


class DocumentStatus(str, enum.Enum):
    pending = "pending"
    processing = "processing"
    ready = "ready"
    failed = "failed"


class Organization(IdMixin, Base):
    __tablename__ = "organizations"
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(120), unique=True)
    default_language: Mapped[str] = mapped_column(String(10), default="fr")
    default_voice_id: Mapped[str] = mapped_column(String(80), default="")
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Paris")
    tone: Mapped[str] = mapped_column(String(80), default="professionnel et chaleureux")
    business_description: Mapped[str] = mapped_column(Text, default="")
    transfer_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class User(IdMixin, Base):
    __tablename__ = "users"
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    full_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    role: Mapped[str] = mapped_column(String(20), default=Role.owner.value)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)


class PhoneNumber(IdMixin, Base):
    __tablename__ = "phone_numbers"
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    number: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    provider: Mapped[str] = mapped_column(String(32), default="twilio")
    direction: Mapped[str] = mapped_column(String(10), default="both")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # Config de l'agent inbound attaché à ce numéro
    greeting: Mapped[str | None] = mapped_column(Text, nullable=True)
    script: Mapped[str | None] = mapped_column(Text, nullable=True)
    knowledge_base_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="SET NULL"), nullable=True)
    voice_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    business_hours: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    allowed_tools: Mapped[list[Any]] = mapped_column(JSON, default=lambda: ["search_knowledge_base", "take_message", "transfer_to_human", "book_appointment"])
    transfer_number: Mapped[str | None] = mapped_column(String(32), nullable=True)


class KnowledgeBase(IdMixin, Base):
    __tablename__ = "knowledge_bases"
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    documents: Mapped[list["Document"]] = relationship(back_populates="knowledge_base", cascade="all, delete-orphan")


class Document(IdMixin, Base):
    __tablename__ = "documents"
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(300))
    source_type: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default=DocumentStatus.pending.value)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    knowledge_base: Mapped[KnowledgeBase] = relationship(back_populates="documents")
    chunks: Mapped[list["Chunk"]] = relationship(back_populates="document", cascade="all, delete-orphan")


class Chunk(IdMixin, Base):
    __tablename__ = "chunks"
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    content: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, default="")
    embedding_id: Mapped[str] = mapped_column(String(64))
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    document: Mapped[Document] = relationship(back_populates="chunks")


class Campaign(IdMixin, Base):
    __tablename__ = "campaigns"
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    objective: Mapped[str] = mapped_column(String(64), default="qualify_lead")
    objective_description: Mapped[str] = mapped_column(Text, default="")
    script: Mapped[str] = mapped_column(Text, default="")
    context: Mapped[str] = mapped_column(Text, default="")
    voice_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    language: Mapped[str] = mapped_column(String(10), default="fr")
    max_concurrency: Mapped[int] = mapped_column(Integer, default=10)
    max_attempts: Mapped[int] = mapped_column(Integer, default=2)
    retry_delay_minutes: Mapped[int] = mapped_column(Integer, default=60)
    schedule: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    voicemail_behavior: Mapped[str] = mapped_column(String(20), default="leave_message")
    voicemail_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    knowledge_base_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="SET NULL"), nullable=True)
    from_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    webhook_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    allowed_tools: Mapped[list[Any]] = mapped_column(JSON, default=lambda: ["search_knowledge_base", "book_appointment", "transfer_to_human"])
    transfer_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=CampaignStatus.draft.value, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Contact(IdMixin, Base):
    __tablename__ = "contacts"
    __table_args__ = (Index("ix_contacts_campaign_status", "campaign_id", "status"),)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    phone: Mapped[str] = mapped_column(String(32), index=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default=ContactStatus.pending.value)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_outcome: Mapped[str | None] = mapped_column(String(40), nullable=True)


class Call(IdMixin, Base):
    __tablename__ = "calls"
    __table_args__ = (Index("ix_calls_org_created", "organization_id", "created_at"),)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True, index=True)
    contact_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True)
    phone_number_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("phone_numbers.id", ondelete="SET NULL"), nullable=True)
    direction: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(20), default=CallStatus.queued.value, index=True)
    provider: Mapped[str] = mapped_column(String(20), default="mock")
    provider_call_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    from_number: Mapped[str] = mapped_column(String(32), default="")
    to_number: Mapped[str] = mapped_column(String(32), default="")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0)
    cost_estimate: Mapped[float] = mapped_column(Float, default=0.0)
    cost_breakdown: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    outcome: Mapped[str | None] = mapped_column(String(40), nullable=True)
    intent: Mapped[str | None] = mapped_column(String(40), nullable=True)
    sentiment: Mapped[float | None] = mapped_column(Float, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_best_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    transcript: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    latency: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class CallEvent(IdMixin, Base):
    __tablename__ = "call_events"
    call_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("calls.id", ondelete="CASCADE"), index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    type: Mapped[str] = mapped_column(String(60))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class AgentDecision(IdMixin, Base):
    __tablename__ = "agent_decisions"
    call_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("calls.id", ondelete="CASCADE"), index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    agent: Mapped[str] = mapped_column(String(40))
    decision: Mapped[str] = mapped_column(String(60))
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    reason: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


__all__ = [
    "Base", "Organization", "User", "PhoneNumber", "KnowledgeBase", "Document", "Chunk", "Campaign", "Contact",
    "Call", "CallEvent", "AgentDecision", "Role", "CampaignStatus", "ContactStatus", "CallStatus", "DocumentStatus", "utcnow",
]
