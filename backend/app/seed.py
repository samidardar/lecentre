"""Données de démo : organisation, admin, base FAQ, numéro entrant, campagne + 20 contacts."""
from __future__ import annotations

import os
import random
from pathlib import Path

from sqlalchemy import select

from app.core.security import hash_password
from app.db import create_all, session_factory
from app.models import Campaign, Contact, Document, KnowledgeBase, Organization, PhoneNumber, User
from app.rag.service import get_rag

ROOT = Path(__file__).resolve().parents[2]
FIRST = ["Alice", "Bruno", "Chloé", "David", "Emma", "Farid", "Gaëlle", "Hugo", "Inès", "Julien"]
LAST = ["Martin", "Bernard", "Dubois", "Thomas", "Robert", "Richard", "Petit", "Durand", "Leroy", "Moreau"]


async def seed() -> dict[str, str]:
    await create_all()
    email = os.getenv("SEED_ADMIN_EMAIL", "demo@callwiz.example.com")
    password = os.getenv("SEED_ADMIN_PASSWORD", "callwiz-demo-2026")
    async with session_factory()() as db:
        user = await db.scalar(select(User).where(User.email == email))
        if user is not None:
            return {"organization_id": str(user.organization_id), "email": email, "status": "already_seeded"}
        org = Organization(name="VéloCity Paris", slug="velocity-paris", tone="professionnel, chaleureux et concis",
                           business_description="Magasin et atelier de vélos électriques à Paris (vente, location, réparation).",
                           transfer_number="+33100000001")
        db.add(org)
        await db.flush()
        db.add(User(email=email, password_hash=hash_password(password), full_name="Admin Démo", organization_id=org.id))
        kb = KnowledgeBase(organization_id=org.id, name="FAQ VéloCity", description="Horaires, tarifs, SAV")
        db.add(kb)
        await db.flush()
        faq_path = ROOT / "examples" / "faq.md"
        doc = Document(organization_id=org.id, knowledge_base_id=kb.id, filename="faq.md", source_type="md")
        db.add(doc)
        db.add(PhoneNumber(organization_id=org.id, number="+33100000000", provider="mock", direction="inbound",
                           label="Standard", knowledge_base_id=kb.id,
                           greeting="Bonjour, VéloCity Paris, je suis l'assistant virtuel. Comment puis-je vous aider ?",
                           business_hours={d: ["09:00", "19:00"] for d in ["mon", "tue", "wed", "thu", "fri", "sat"]}))
        camp = Campaign(organization_id=org.id, name="Relance révision annuelle", objective="schedule_meeting",
                        objective_description="Proposer à {first_name} la révision annuelle de son vélo (offerte la 1re année).",
                        script="Rappeler que le vélo de {first_name} a été acheté il y a un an. Proposer un créneau d'atelier.",
                        knowledge_base_id=kb.id, max_concurrency=10)
        db.add(camp)
        await db.flush()
        rnd = random.Random(42)
        for i in range(20):
            db.add(Contact(organization_id=org.id, campaign_id=camp.id, first_name=rnd.choice(FIRST), last_name=rnd.choice(LAST),
                           phone=f"+3361{i:07d}", attributes={"modele": rnd.choice(["Urbain E1", "Cargo C2", "Trek T5"])}))
        await db.commit()
        doc_id, org_id = doc.id, org.id
    await get_rag().ingest(doc_id, "faq.md", faq_path.read_bytes())
    return {"organization_id": str(org_id), "email": email, "status": "seeded"}
