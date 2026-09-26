"""Import CSV de contacts avec détection automatique des colonnes (surchargeable par `mapping`)."""
from __future__ import annotations

import csv
import io
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Contact
from app.schemas import normalize_phone

ALIASES = {
    "phone": {"phone", "telephone", "téléphone", "tel", "tél", "mobile", "portable", "numero", "numéro", "phone_number", "number"},
    "first_name": {"first_name", "firstname", "prenom", "prénom", "given_name"},
    "last_name": {"last_name", "lastname", "nom", "surname", "family_name"},
    "email": {"email", "e-mail", "mail", "courriel"},
}
MAX_ROWS = 50_000


def detect_mapping(columns: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for col in columns:
        key = col.strip().lower()
        for field, aliases in ALIASES.items():
            if key in aliases and field not in mapping:
                mapping[field] = col
    return mapping


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore")


async def import_contacts(db: AsyncSession, *, organization_id: uuid.UUID, campaign_id: uuid.UUID, data: bytes,
                          mapping: dict[str, str] | None = None, default_cc: str = "+33") -> dict[str, Any]:
    text = _decode(data)
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    columns = [c for c in (reader.fieldnames or []) if c]
    used = detect_mapping(columns) | (mapping or {})
    if "phone" not in used:
        return {"imported": 0, "skipped": 0, "errors": [{"row": 0, "error": "colonne téléphone introuvable"}],
                "detected_columns": columns, "mapping_used": used}

    existing = set((await db.scalars(select(Contact.phone).where(Contact.campaign_id == campaign_id))).all())
    mapped_cols = set(used.values())
    imported, skipped, errors = 0, 0, []
    batch: list[Contact] = []
    for i, row in enumerate(reader, start=2):
        if i > MAX_ROWS + 1:
            errors.append({"row": i, "error": f"limite de {MAX_ROWS} lignes atteinte"})
            break
        try:
            phone = normalize_phone(row.get(used["phone"], ""), default_cc)
        except ValueError as exc:
            skipped += 1
            if len(errors) < 100:
                errors.append({"row": i, "error": str(exc)})
            continue
        if phone in existing:
            skipped += 1
            continue
        existing.add(phone)
        batch.append(Contact(
            organization_id=organization_id, campaign_id=campaign_id, phone=phone,
            first_name=(row.get(used.get("first_name", ""), "") or "").strip() or None,
            last_name=(row.get(used.get("last_name", ""), "") or "").strip() or None,
            email=(row.get(used.get("email", ""), "") or "").strip() or None,
            attributes={k: v for k, v in row.items() if k and k not in mapped_cols and v not in (None, "")},
        ))
        imported += 1
        if len(batch) >= 1000:
            db.add_all(batch)
            await db.flush()
            batch = []
    db.add_all(batch)
    await db.commit()
    return {"imported": imported, "skipped": skipped, "errors": errors, "detected_columns": columns, "mapping_used": used}
