"""Commandes d'exploitation : diagnostic des services (doctor), branchement Twilio, démarrage tout-en-un (up)."""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import typer

from app.core.config import Settings, get_settings

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
TUNNEL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


@dataclass
class Check:
    name: str
    ok: bool | None  # None = non configuré
    detail: str
    hint: str = ""


# ================================================================== doctor
async def _get(client: httpx.AsyncClient, url: str, **kw: Any) -> httpx.Response:
    return await client.get(url, timeout=15, **kw)


async def run_checks(s: Settings, live: bool = False) -> list[Check]:
    checks: list[Check] = []
    async with httpx.AsyncClient() as client:
        # Base de données
        try:
            from sqlalchemy import text

            from app.db import get_engine

            async with get_engine().connect() as conn:
                await conn.execute(text("SELECT 1"))
            checks.append(Check("Base de données", True, s.database_url.split("://")[0]))
        except Exception as exc:
            checks.append(Check("Base de données", False, str(exc)[:120], "Vérifiez DATABASE_URL, puis `callwiz db migrate`."))

        checks.append(Check("Mode voix", True, f"LIVE_PROVIDER={s.live_provider}, LLM_PROVIDER={s.llm_provider}, TELEPHONY_PROVIDER={s.telephony_provider}",
                            "" if s.live_provider == "cascade" else "LIVE_PROVIDER=mock : réponses simulées, aucun vrai appel IA."))

        # LLM
        if s.llm_provider == "openrouter":
            if not s.openrouter_api_key:
                checks.append(Check("OpenRouter", None, "clé absente", "OPENROUTER_API_KEY dans backend/.env (openrouter.ai → Keys)."))
            else:
                r = await _get(client, "https://openrouter.ai/api/v1/key", headers={"Authorization": f"Bearer {s.openrouter_api_key}"})
                if r.status_code == 200:
                    d = r.json().get("data", {})
                    limit = d.get("limit_remaining", d.get("limit"))
                    checks.append(Check("OpenRouter", True, f"clé valide · crédit restant : {limit if limit is not None else 'illimité'} · modèle {s.openrouter_model}"))
                    if live:
                        checks.append(await _llm_latency(s))
                else:
                    checks.append(Check("OpenRouter", False, f"HTTP {r.status_code}", "Clé invalide ou révoquée."))
        else:
            checks.append(Check("Anthropic", bool(s.anthropic_api_key) or None, "clé présente" if s.anthropic_api_key else "clé absente",
                                "" if s.anthropic_workspace_id or not s.anthropic_api_key else "Ajoutez ANTHROPIC_WORKSPACE_ID si la clé n'est pas rattachée à un workspace."))

        # Deepgram
        if not s.deepgram_api_key:
            checks.append(Check("Deepgram (écoute)", None, "clé absente", "DEEPGRAM_API_KEY (console.deepgram.com)."))
        else:
            r = await _get(client, "https://api.deepgram.com/v1/projects", headers={"Authorization": f"Token {s.deepgram_api_key}"})
            checks.append(Check("Deepgram (écoute)", r.status_code == 200, "clé valide" if r.status_code == 200 else f"HTTP {r.status_code}"))

        # Fish Audio
        if not s.fish_api_key:
            checks.append(Check("Fish Audio (voix)", None, "clé absente", "FISH_API_KEY (fish.audio/app/developers)."))
        else:
            r = await _get(client, "https://api.fish.audio/model", params={"page_size": 1}, headers={"Authorization": f"Bearer {s.fish_api_key}"})
            if r.status_code != 200:
                checks.append(Check("Fish Audio (voix)", False, f"HTTP {r.status_code}", "Clé invalide."))
            else:
                detail = f"clé valide · modèle {s.fish_model} · voix {s.fish_voice_id or 'par défaut'}"
                if live:
                    t = await client.post("https://api.fish.audio/v1/tts", timeout=60,
                                          headers={"Authorization": f"Bearer {s.fish_api_key}", "model": s.fish_model},
                                          json={"text": "Test.", "reference_id": s.fish_voice_id, "format": "mp3"})
                    if t.status_code == 402:
                        checks.append(Check("Fish Audio (voix)", False, detail + " · crédit API épuisé",
                                            "Ajoutez du crédit API, ou FISH_MODEL=s2.1-pro-free pour tester."))
                        detail = ""
                    elif t.status_code != 200:
                        checks.append(Check("Fish Audio (voix)", False, detail + f" · synthèse HTTP {t.status_code}"))
                        detail = ""
                    else:
                        detail += " · synthèse OK"
                if detail:
                    checks.append(Check("Fish Audio (voix)", True, detail))

        # Twilio
        checks += await _twilio_checks(client, s)

        # URL publique
        if s.public_base_url.startswith("http://localhost") or s.public_base_url.startswith("http://127."):
            checks.append(Check("URL publique", None, s.public_base_url,
                                "Twilio ne peut pas joindre localhost : lancez `callwiz up` (tunnel automatique) ou hébergez le serveur."))
        else:
            try:
                r = await _get(client, s.public_base_url.rstrip("/") + "/health")
                checks.append(Check("URL publique", r.status_code == 200, f"{s.public_base_url} → HTTP {r.status_code}"))
            except Exception as exc:
                checks.append(Check("URL publique", False, f"{s.public_base_url} injoignable ({type(exc).__name__})",
                                    "Le serveur ou le tunnel est arrêté : `callwiz up`."))
    return checks


async def _llm_latency(s: Settings) -> Check:
    from app.llm.openrouter import OpenRouterConversation

    llm = OpenRouterConversation(s)
    t0 = time.perf_counter()
    first: float | None = None
    text = ""
    try:
        async for piece in llm.stream("Tu es un réceptionniste. Réponds en une phrase courte, en français.", [],
                                      [{"role": "user", "content": "Bonjour, vous êtes ouverts samedi ?"}]):
            if isinstance(piece, str):
                first = first or time.perf_counter()
                text += piece
        return Check("OpenRouter · latence", True, f"premier mot {int(((first or time.perf_counter()) - t0) * 1000)} ms · « {text.strip()[:60]} »")
    except Exception as exc:
        return Check("OpenRouter · latence", False, str(exc)[:160])
    finally:
        await llm.aclose()


async def _twilio_checks(client: httpx.AsyncClient, s: Settings) -> list[Check]:
    if not (s.twilio_account_sid and s.twilio_auth_token):
        return [Check("Twilio", None, "identifiants absents", "TWILIO_ACCOUNT_SID et TWILIO_AUTH_TOKEN.")]
    base = f"https://api.twilio.com/2010-04-01/Accounts/{s.twilio_account_sid}"
    auth = (s.twilio_account_sid, s.twilio_auth_token)
    r = await _get(client, f"{base}.json", auth=auth)
    if r.status_code != 200:
        return [Check("Twilio", False, f"HTTP {r.status_code}", "Account SID / Auth Token invalides.")]
    acc = r.json()
    out = [Check("Twilio", True, f"compte {acc.get('status')} ({acc.get('type')})",
                 "Compte d'essai : n'appelle que les numéros vérifiés (Verified Caller IDs)." if acc.get("type") == "Trial" else "")]
    nums = (await _get(client, f"{base}/IncomingPhoneNumbers.json", auth=auth)).json().get("incoming_phone_numbers", [])
    if not nums:
        out.append(Check("Twilio · numéro", False, "aucun numéro", "Console Twilio → Phone Numbers → Buy a number."))
    else:
        expected = s.public_base_url.rstrip("/") + "/telephony/twilio/voice"
        for n in nums:
            wired = (n.get("voice_url") or "") == expected
            out.append(Check(f"Twilio · {n['phone_number']}", wired, "relié au serveur" if wired else f"webhook : {n.get('voice_url') or 'aucun'}",
                             "" if wired else "`callwiz twilio connect` (ou `callwiz up`)."))
    bal = await _get(client, f"{base}/Balance.json", auth=auth)
    if bal.status_code == 200:
        amount = float(bal.json().get("balance") or 0)
        out.append(Check("Twilio · crédit", amount > 0, f"{amount:.2f} {bal.json().get('currency', '')}", "" if amount > 0 else "Ajoutez du crédit (Billing)."))
    return out


def print_checks(checks: list[Check]) -> int:
    failures = 0
    for c in checks:
        mark = "✓" if c.ok else ("–" if c.ok is None else "✗")
        failures += c.ok is False
        typer.echo(f" {mark} {c.name:<26} {c.detail}")
        if c.hint:
            typer.echo(f"   → {c.hint}")
    return failures


# ================================================================== twilio connect
async def twilio_connect(s: Settings, number: str | None = None) -> str:
    """Relie un numéro Twilio au serveur (webhook voix) et l'enregistre comme numéro entrant de l'organisation."""
    from sqlalchemy import select

    from app.db import create_all, session_factory
    from app.models import KnowledgeBase, Organization, PhoneNumber

    if not (s.twilio_account_sid and s.twilio_auth_token):
        raise typer.BadParameter("TWILIO_ACCOUNT_SID et TWILIO_AUTH_TOKEN requis")
    base = f"https://api.twilio.com/2010-04-01/Accounts/{s.twilio_account_sid}"
    auth = (s.twilio_account_sid, s.twilio_auth_token)
    async with httpx.AsyncClient(auth=auth, timeout=20) as client:
        nums = (await client.get(f"{base}/IncomingPhoneNumbers.json")).json().get("incoming_phone_numbers", [])
        target = next((n for n in nums if not number or n["phone_number"] == number), None)
        if target is None:
            raise typer.BadParameter("aucun numéro Twilio trouvé" + (f" pour {number}" if number else " : achetez-en un dans la console Twilio"))
        r = await client.post(f"{base}/IncomingPhoneNumbers/{target['sid']}.json", data={
            "VoiceUrl": s.public_base_url.rstrip("/") + "/telephony/twilio/voice", "VoiceMethod": "POST",
        })
        r.raise_for_status()
    await create_all()
    async with session_factory()() as db:
        pn = await db.scalar(select(PhoneNumber).where(PhoneNumber.number == target["phone_number"]))
        if pn is None:
            org = await db.scalar(select(Organization).order_by(Organization.created_at))
            if org is None:
                raise typer.BadParameter("aucune organisation : `callwiz db seed` ou créez un compte dans la console")
            kb = await db.scalar(select(KnowledgeBase).where(KnowledgeBase.organization_id == org.id).order_by(KnowledgeBase.created_at))
            db.add(PhoneNumber(organization_id=org.id, number=target["phone_number"], provider="twilio", direction="both",
                               label="Twilio", knowledge_base_id=kb.id if kb else None))
        else:
            pn.provider, pn.active = "twilio", True
        await db.commit()
    if not s.twilio_default_from:
        set_env("TWILIO_DEFAULT_FROM", target["phone_number"])
    return target["phone_number"]


# ================================================================== up
def set_env(key: str, value: str, env_file: Path = BACKEND / ".env") -> None:
    """Écrit/remplace KEY=value dans backend/.env et dans l'environnement courant."""
    lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
    out, done = [], False
    for line in lines:
        if line.split("=", 1)[0].strip() == key:
            out.append(f"{key}={value}")
            done = True
        else:
            out.append(line)
    if not done:
        out.append(f"{key}={value}")
    env_file.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.environ[key] = value
    get_settings.cache_clear()


def find_cloudflared() -> str | None:
    for cand in [os.environ.get("CLOUDFLARED_PATH"), str(ROOT / "tools" / "cloudflared.exe"), str(ROOT / "tools" / "cloudflared"),
                 shutil.which("cloudflared")]:
        if cand and Path(cand).exists():
            return cand
    return None


def start_tunnel(port: int, timeout: float = 40) -> tuple[subprocess.Popen[str], str]:
    exe = find_cloudflared()
    if exe is None:
        raise typer.BadParameter("cloudflared introuvable : placez-le dans tools/ ou définissez CLOUDFLARED_PATH")
    proc = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="ignore")
    found: list[str] = []

    def reader() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            m = TUNNEL_RE.search(line)
            if m and not found:
                found.append(m.group(0))

    threading.Thread(target=reader, daemon=True).start()
    deadline = time.monotonic() + timeout
    while not found and time.monotonic() < deadline and proc.poll() is None:
        time.sleep(0.2)
    if not found:
        proc.terminate()
        raise typer.BadParameter("le tunnel n'a pas fourni d'URL publique")
    return proc, found[0]


def up(port: int = 8000, tunnel: bool = True, connect_twilio: bool = True) -> None:
    import uvicorn

    proc = None
    try:
        if tunnel:
            proc, url = start_tunnel(port)
            set_env("PUBLIC_BASE_URL", url)
            typer.echo(f"✓ Tunnel public : {url}")
        s = get_settings()
        if connect_twilio and s.telephony_provider == "twilio" and s.twilio_account_sid and s.twilio_auth_token:
            try:
                num = asyncio.run(twilio_connect(s))
                typer.echo(f"✓ Numéro Twilio {num} relié au serveur")
            except Exception as exc:
                typer.echo(f"– Twilio non relié : {exc}")
        typer.echo(f"✓ Console : {s.public_base_url.rstrip('/')}/console  (local : http://127.0.0.1:{port}/console)")
        uvicorn.run("app.main:app", host="127.0.0.1", port=port, ws_ping_interval=20, log_config=None)
    finally:
        if proc is not None:
            proc.terminate()
