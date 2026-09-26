"""CLI CallWiz : piloter la plateforme sans l'UI.

    callwiz dev | worker | init
    callwiz db migrate | seed
    callwiz campaign create | launch | pause | stop | list
    callwiz call test
    callwiz rag ingest | query
    callwiz analytics tail | monitor live
    callwiz loadtest --calls 100
    callwiz gdpr purge
"""
from __future__ import annotations

import asyncio
import json
import secrets
import statistics
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import typer

app = typer.Typer(help="CallWiz AI — centre d'appels IA", no_args_is_help=True)
db_app = typer.Typer(help="Base de données")
campaign_app = typer.Typer(help="Campagnes sortantes")
call_app = typer.Typer(help="Appels")
rag_app = typer.Typer(help="Base de connaissances (RAG)")
analytics_app = typer.Typer(help="Analytics temps réel")
monitor_app = typer.Typer(help="Supervision")
gdpr_app = typer.Typer(help="RGPD")
for sub, name in [(db_app, "db"), (campaign_app, "campaign"), (call_app, "call"), (rag_app, "rag"),
                  (analytics_app, "analytics"), (monitor_app, "monitor"), (gdpr_app, "gdpr")]:
    app.add_typer(sub, name=name)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def echo_json(data: Any) -> None:
    typer.echo(json.dumps(data, indent=2, ensure_ascii=False, default=str))


async def _default_org(org: Optional[str]) -> uuid.UUID:
    from sqlalchemy import select

    from app.db import create_all, session_factory
    from app.models import Organization

    await create_all()
    if org:
        return uuid.UUID(org)
    async with session_factory()() as db:
        first = await db.scalar(select(Organization).order_by(Organization.created_at))
    if first is None:
        raise typer.BadParameter("aucune organisation : lancez `callwiz db seed` ou passez --org")
    return first.id


async def _with_runtime(fn: Any) -> Any:
    """Démarre bus + analytics + dispatcher dans ce process (mode CLI autonome)."""
    from app.agents.analytics import analytics_agent
    from app.agents.mcp_client import mcp_manager
    from app.core.logging import setup_logging
    from app.db import create_all
    from app.services.call_manager import get_call_manager
    from app.services.dispatcher import get_dispatcher

    setup_logging("WARNING")
    await create_all()
    task = asyncio.create_task(analytics_agent.run())
    await mcp_manager.start()
    try:
        return await fn(get_call_manager(), get_dispatcher())
    finally:
        await get_dispatcher().shutdown()
        await get_call_manager().shutdown()
        await analytics_agent.stop()
        task.cancel()


# ------------------------------------------------------------------ serveur
@app.command()
def dev(host: str = "0.0.0.0", port: int = 8000, reload: bool = False) -> None:
    """Lance l'API (REST + WebSocket + webhooks Twilio + dispatcher)."""
    import uvicorn

    uvicorn.run("app.main:app", host=host, port=port, reload=reload, ws_ping_interval=20, log_config=None)


@app.command()
def worker() -> None:
    """Lance uniquement le dispatcher de campagnes (scale-out : l'API reçoit les médias Twilio)."""

    async def main(_: Any, dispatcher: Any) -> None:
        await dispatcher.recover()
        typer.echo("worker démarré — Ctrl+C pour arrêter")
        while True:
            from sqlalchemy import select

            from app.db import session_factory
            from app.models import Campaign, CampaignStatus

            async with session_factory()() as db:
                for cid in (await db.scalars(select(Campaign.id).where(Campaign.status == CampaignStatus.running.value))).all():
                    dispatcher.ensure_running(cid)
            await asyncio.sleep(2)

    run(_with_runtime(main))


@app.command()
def init(path: Path = Path(".env")) -> None:
    """Crée un .env local (secret JWT aléatoire) à partir de .env.example."""
    example = Path(__file__).resolve().parents[2] / ".env.example"
    if path.exists():
        typer.echo(f"{path} existe déjà")
        raise typer.Exit()
    content = example.read_text(encoding="utf-8") if example.exists() else ""
    content = content.replace("JWT_SECRET=change-me", f"JWT_SECRET={secrets.token_urlsafe(48)}")
    path.write_text(content, encoding="utf-8")
    typer.echo(f"{path} créé")


# ------------------------------------------------------------------ db
@db_app.command("migrate")
def db_migrate() -> None:
    """Applique les migrations Alembic."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "alembic"))
    command.upgrade(cfg, "head")
    typer.echo("migrations appliquées")


@db_app.command("seed")
def db_seed() -> None:
    """Crée l'organisation de démo (identifiants : SEED_ADMIN_EMAIL / SEED_ADMIN_PASSWORD, cf. .env.example)."""
    from app.seed import seed

    echo_json(run(seed()))


# ------------------------------------------------------------------ campagnes
@campaign_app.command("list")
def campaign_list(org: Optional[str] = None) -> None:
    async def main() -> list[dict]:
        from sqlalchemy import select

        from app.db import session_factory
        from app.models import Campaign

        org_id = await _default_org(org)
        async with session_factory()() as db:
            rows = (await db.scalars(select(Campaign).where(Campaign.organization_id == org_id))).all()
        return [{"id": str(c.id), "name": c.name, "status": c.status, "objective": c.objective} for c in rows]

    echo_json(run(main()))


@campaign_app.command("create")
def campaign_create(name: str = typer.Option(...), objective: str = "qualify_lead", script: str = "", context: str = "",
                    concurrency: int = 10, org: Optional[str] = None) -> None:
    async def main() -> str:
        from app.db import session_factory
        from app.models import Campaign

        org_id = await _default_org(org)
        async with session_factory()() as db:
            c = Campaign(organization_id=org_id, name=name, objective=objective, script=script, context=context, max_concurrency=concurrency)
            db.add(c)
            await db.commit()
            return str(c.id)

    typer.echo(run(main()))


@campaign_app.command("launch")
def campaign_launch(file: Path = typer.Option(..., exists=True), concurrency: int = 10, name: str = "Campagne CLI",
                    objective: str = "qualify_lead", script: str = "", org: Optional[str] = None, campaign: Optional[str] = None) -> None:
    """Importe un CSV, lance la campagne et suit la progression jusqu'à la fin."""

    async def main(_: Any, dispatcher: Any) -> dict:
        from app.db import session_factory
        from app.models import Campaign
        from app.services.contact_import import import_contacts
        from app.services.dispatcher import campaign_progress

        org_id = await _default_org(org)
        dispatcher.respect_schedule = False
        async with session_factory()() as db:
            if campaign:
                camp = await db.get(Campaign, uuid.UUID(campaign))
            else:
                camp = Campaign(organization_id=org_id, name=name, objective=objective, script=script, max_concurrency=concurrency)
                db.add(camp)
                await db.commit()
            assert camp is not None
            res = await import_contacts(db, organization_id=org_id, campaign_id=camp.id, data=file.read_bytes())
            camp_id = camp.id
        typer.echo(f"campagne {camp_id} : {res['imported']} contacts importés, {res['skipped']} ignorés")
        await dispatcher.start(camp_id)
        while True:
            p = await campaign_progress(camp_id, dispatcher.active_calls(camp_id))
            typer.echo(f"\r{p['percent']:5.1f}%  actifs={p['active_calls']:3d}  terminés={p['completed']}  échecs={p['failed']}  "
                       f"succès={p['success_count']}", nl=False)
            if p["status"] in ("completed", "stopped", "failed"):
                typer.echo("")
                return p
            await asyncio.sleep(0.5)

    echo_json(run(_with_runtime(main)))


def _set_status(campaign_id: str, status: str) -> None:
    async def main(_: Any, dispatcher: Any) -> None:
        from app.models import CampaignStatus

        await dispatcher.set_status(uuid.UUID(campaign_id), CampaignStatus(status))

    run(_with_runtime(main))
    typer.echo(f"{campaign_id} → {status}")


@campaign_app.command("pause")
def campaign_pause(campaign_id: str) -> None:
    _set_status(campaign_id, "paused")


@campaign_app.command("stop")
def campaign_stop(campaign_id: str) -> None:
    _set_status(campaign_id, "stopped")


# ------------------------------------------------------------------ appels
@call_app.command("test")
def call_test(agent: str = typer.Option("inbound", help="inbound | outbound"), to: Optional[str] = None,
              say: list[str] = typer.Option(None, help="Répliques de l'appelant simulé (répétable)"),
              campaign: Optional[str] = None, org: Optional[str] = None) -> None:
    """Appel de test. Sans --to : appel simulé (texte) affiché en direct. Avec --to : vrai appel Twilio sortant."""

    async def main(manager: Any, _: Any) -> dict:
        from sqlalchemy import select

        from app.core import events as ev
        from app.db import session_factory
        from app.models import Call, PhoneNumber
        from app.voice.transport import SimulatedCallerTransport

        org_id = await _default_org(org)
        async with session_factory()() as db:
            pn = await db.scalar(select(PhoneNumber).where(PhoneNumber.organization_id == org_id))
        camp_id = uuid.UUID(campaign) if campaign else None
        if to:
            call = await manager.create_call(organization_id=org_id, direction="outbound", from_number="", to_number=to, campaign_id=camp_id)
            await manager.dial(call)
            typer.echo(f"appel {call.id} lancé vers {to} (suivi : callwiz monitor live)")
            return {"call_id": str(call.id)}
        script = say or ["Bonjour, quels sont vos horaires d'ouverture ?", "Et combien coûte une révision ?", "Merci, au revoir."]
        call = await manager.create_call(organization_id=org_id, direction=agent, from_number="+33600000000",
                                         to_number=pn.number if pn else "+33100000000", campaign_id=camp_id,
                                         phone_number_id=pn.id if (pn and agent == "inbound") else None)

        async def show() -> None:
            async for e in ev.get_bus().subscribe():
                if e.call_id != str(call.id):
                    continue
                if e.event in (ev.CALL_TRANSCRIPT_FINAL, ev.CALL_RESPONSE_GENERATED):
                    who = "👤" if e.payload.get("role") == "user" else "🤖"
                    typer.echo(f"{who} {e.payload.get('text')}")
                elif e.event == ev.CALL_TOOL_CALLED:
                    typer.echo(f"   🔧 {e.payload['tool']}({json.dumps(e.payload.get('args'), ensure_ascii=False)}) {e.payload.get('duration_ms')} ms")
                elif e.event == ev.CALL_LATENCY:
                    typer.echo(f"   ⏱  voix→voix {e.payload['voice_to_voice_ms']} ms")

        viewer = asyncio.create_task(show())
        await manager.start_session(call.id, SimulatedCallerTransport(script, think_time=0.3))
        await asyncio.sleep(0.3)
        viewer.cancel()
        async with session_factory()() as db:
            c = await db.get(Call, call.id)
            return {"call_id": str(call.id), "status": c.status, "outcome": c.outcome, "intent": c.intent,
                    "sentiment": c.sentiment, "cost": c.cost_estimate, "latency": c.latency.get("avg_ms"), "summary": c.summary}

    echo_json(run(_with_runtime(main)))


# ------------------------------------------------------------------ RAG
@rag_app.command("ingest")
def rag_ingest(file: Path = typer.Option(..., exists=True), kb: Optional[str] = None, org: Optional[str] = None) -> None:
    async def main() -> dict:
        from sqlalchemy import select

        from app.db import session_factory
        from app.models import Document, KnowledgeBase
        from app.rag.parsing import source_type_for
        from app.rag.service import get_rag

        org_id = await _default_org(org)
        async with session_factory()() as db:
            base = await db.get(KnowledgeBase, uuid.UUID(kb)) if kb else await db.scalar(
                select(KnowledgeBase).where(KnowledgeBase.organization_id == org_id).order_by(KnowledgeBase.created_at))
            if base is None:
                base = KnowledgeBase(organization_id=org_id, name="Base CLI")
                db.add(base)
                await db.flush()
            doc = Document(organization_id=org_id, knowledge_base_id=base.id, filename=file.name, source_type=source_type_for(file.name))
            db.add(doc)
            await db.commit()
            ids = (str(base.id), doc.id)
        n = await get_rag().ingest(ids[1], file.name, file.read_bytes())
        return {"knowledge_base_id": ids[0], "document_id": str(ids[1]), "chunks": n}

    echo_json(run(main()))


@rag_app.command("query")
def rag_query(query: str, kb: Optional[str] = None, org: Optional[str] = None, top_k: int = 4) -> None:
    async def main() -> dict:
        from app.rag.service import get_rag

        org_id = await _default_org(org)
        res = await get_rag().search(str(org_id), query, [kb] if kb else None, top_k=top_k)
        return {"query": query, "low_confidence": res.low_confidence, "latency_ms": round(res.latency_ms, 2),
                "results": [{"score": round(h.score, 4), "source": h.source, "content": h.content[:300]} for h in res.hits]}

    echo_json(run(main()))


# ------------------------------------------------------------------ temps réel (via l'API en cours d'exécution)
async def _tail(path: str, url: str, email: str, password: str, fmt: Any) -> None:
    import httpx
    import websockets

    async with httpx.AsyncClient(base_url=url) as client:
        r = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
        r.raise_for_status()
        token = r.json()["access_token"]
    ws_url = url.replace("http", "ws", 1) + f"{path}?token={token}"
    async with websockets.connect(ws_url) as ws:
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("event") not in ("ping", "pong"):
                typer.echo(fmt(msg))


@analytics_app.command("tail")
def analytics_tail(url: str = "http://localhost:8000", email: str = typer.Option(..., envvar="SEED_ADMIN_EMAIL"),
                   password: str = typer.Option(..., envvar="SEED_ADMIN_PASSWORD")) -> None:
    """Affiche les snapshots analytics en direct (serveur `callwiz dev` requis)."""

    def fmt(m: dict) -> str:
        p = m.get("payload", {})
        return (f"actifs={p.get('active_calls')} aujourd'hui={p.get('calls_today')} ok={p.get('completed_today')} "
                f"échecs={p.get('failed_today')} coût={p.get('cost_today')}€ latence={p.get('avg_latency_ms')}ms "
                f"utilisation={p.get('utilization')}")

    run(_tail("/ws/analytics", url, email, password, fmt))


@monitor_app.command("live")
def monitor_live(url: str = "http://localhost:8000", email: str = typer.Option(..., envvar="SEED_ADMIN_EMAIL"),
                 password: str = typer.Option(..., envvar="SEED_ADMIN_PASSWORD")) -> None:
    """Flux d'événements d'appels en direct."""

    def fmt(m: dict) -> str:
        p = m.get("payload", {})
        detail = p.get("text") or p.get("tool") or p.get("status") or p.get("decision") or ""
        return f"{m.get('timestamp', '')[11:19]} {str(m.get('call_id'))[:8]} {m['event']:<26} {detail}"

    run(_tail("/ws/calls", url, email, password, fmt))


# ------------------------------------------------------------------ charge
@app.command()
def loadtest(calls: int = 100, first_audio_ms: int = 250, org: Optional[str] = None) -> None:
    """Campagne mock de N appels simultanés : mesure débit, pic de concurrence et latence voix→voix (modèle simulé)."""
    from app.core.config import get_settings

    get_settings().telephony_calls_per_second = max(get_settings().telephony_calls_per_second, calls)

    async def main(manager: Any, dispatcher: Any) -> dict:
        from sqlalchemy import select

        from app.db import session_factory
        from app.live import set_live_model
        from app.live.mock import MockLiveModel
        from app.models import Call, Campaign, Contact
        from app.services.dispatcher import campaign_progress
        from app.telephony.mock_provider import MockTelephonyProvider

        if not isinstance(manager.telephony, MockTelephonyProvider):
            raise typer.BadParameter("loadtest exige TELEPHONY_PROVIDER=mock")
        set_live_model(MockLiveModel(first_audio_delay=first_audio_ms / 1000, realtime_factor=1.0))
        manager.telephony.think_time = 0.5
        dispatcher.respect_schedule = False
        dispatcher.poll_interval_s = 0.1
        org_id = await _default_org(org)
        async with session_factory()() as db:
            camp = Campaign(organization_id=org_id, name=f"loadtest-{calls}", max_concurrency=min(calls, 100), max_attempts=1)
            db.add(camp)
            await db.flush()
            db.add_all(Contact(organization_id=org_id, campaign_id=camp.id, first_name=f"Test{i}", phone=f"+3369{i:07d}") for i in range(calls))
            await db.commit()
            camp_id = camp.id
        t0, peak = time.perf_counter(), 0
        await dispatcher.start(camp_id)
        while True:
            peak = max(peak, manager.local_active)
            p = await campaign_progress(camp_id, dispatcher.active_calls(camp_id))
            if p["status"] != "running":
                break
            await asyncio.sleep(0.05)
        elapsed = time.perf_counter() - t0
        async with session_factory()() as db:
            rows = (await db.scalars(select(Call).where(Call.campaign_id == camp_id))).all()
        lat = sorted(x for c in rows for x in (c.latency or {}).get("voice_to_voice_ms", []))
        return {
            "calls": len(rows), "failed": sum(c.status == "failed" for c in rows), "peak_concurrency": peak,
            "elapsed_s": round(elapsed, 1), "avg_cost_eur": round(statistics.mean(c.cost_estimate for c in rows), 4) if rows else 0,
            "voice_to_voice_ms": {"p50": lat[len(lat) // 2] if lat else None, "p95": lat[int(len(lat) * 0.95)] if lat else None,
                                  "samples": len(lat), "note": f"modèle simulé ({first_audio_ms} ms) : mesure l'overhead de la plateforme"},
        }

    echo_json(run(_with_runtime(main)))


@gdpr_app.command("purge")
def gdpr_purge(days: Optional[int] = None) -> None:
    """Supprime transcripts et événements plus anciens que RETENTION_DAYS."""

    async def main() -> dict:
        from datetime import datetime, timedelta, timezone

        from sqlalchemy import delete, update

        from app.core.config import get_settings
        from app.db import create_all, session_factory
        from app.models import Call, CallEvent

        await create_all()
        cutoff = datetime.now(timezone.utc) - timedelta(days=days or get_settings().retention_days)
        async with session_factory()() as db:
            r1 = await db.execute(update(Call).where(Call.created_at < cutoff).values(transcript=None, summary=None))
            r2 = await db.execute(delete(CallEvent).where(CallEvent.created_at < cutoff))
            await db.commit()
        return {"calls_anonymized": r1.rowcount, "events_deleted": r2.rowcount, "cutoff": cutoff.isoformat()}

    echo_json(run(main()))


if __name__ == "__main__":
    app()
