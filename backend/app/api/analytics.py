from __future__ import annotations

import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from app.agents.analytics import analytics_agent
from app.api.deps import DB, CurrentPrincipal, get_owned
from app.core.config import get_settings
from app.models import Call, CallEvent, CallStatus, Campaign, CampaignStatus, Contact
from app.schemas import AnalyticsOverview, CampaignAnalytics, RealtimeSnapshot

router = APIRouter(prefix="/analytics", tags=["analytics"])
SUCCESS = {"success"}


def _pctl(values: list[float], q: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    return round(values[min(len(values) - 1, int(len(values) * q))], 1)


@router.get("/overview", response_model=AnalyticsOverview)
async def overview(p: CurrentPrincipal, db: DB, days: int = Query(7, ge=1, le=365)) -> AnalyticsOverview:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    calls = (await db.execute(
        select(Call.status, Call.outcome, Call.intent, Call.sentiment, Call.duration_seconds, Call.cost_estimate, Call.latency, Call.created_at)
        .where(Call.organization_id == p.org_id, Call.created_at >= since)
    )).all()
    total = len(calls)
    by_status = Counter(c.status for c in calls)
    outcomes = Counter(c.outcome for c in calls if c.outcome)
    answered = [c for c in calls if c.duration_seconds > 0]
    sentiments = [c.sentiment for c in calls if c.sentiment is not None]
    latencies = [x for c in calls for x in ((c.latency or {}).get("voice_to_voice_ms") or [])]
    per_hour = Counter(c.created_at.replace(minute=0, second=0, microsecond=0).isoformat() for c in calls)
    running = await db.scalar(select(func.count()).select_from(Campaign).where(
        Campaign.organization_id == p.org_id, Campaign.status == CampaignStatus.running.value))
    snap = analytics_agent.snapshot(str(p.org_id))
    return AnalyticsOverview(
        period_days=days, total_calls=total, completed_calls=by_status.get("completed", 0), failed_calls=by_status.get("failed", 0),
        transferred_calls=by_status.get("transferred", 0), active_calls=by_status.get("active", 0) + by_status.get("ringing", 0),
        queued_calls=by_status.get("queued", 0),
        average_duration_s=round(sum(c.duration_seconds for c in answered) / len(answered), 1) if answered else 0.0,
        average_cost=round(sum(c.cost_estimate for c in calls) / total, 4) if total else 0.0,
        total_cost=round(sum(c.cost_estimate for c in calls), 4),
        average_sentiment=round(sum(sentiments) / len(sentiments), 3) if sentiments else None,
        conversion_rate=round(sum(outcomes[o] for o in SUCCESS) / len(answered), 3) if answered else 0.0,
        transfer_rate=round(by_status.get("transferred", 0) / len(answered), 3) if answered else 0.0,
        average_voice_latency_ms=round(sum(latencies) / len(latencies), 1) if latencies else snap["avg_latency_ms"],
        p95_voice_latency_ms=_pctl(latencies, 0.95),
        top_intents=[{"intent": k, "count": v} for k, v in Counter(c.intent for c in calls if c.intent).most_common(8)],
        outcomes=dict(outcomes), calls_per_hour=[{"hour": h, "count": n} for h, n in sorted(per_hour.items())],
        running_campaigns=int(running or 0),
    )


@router.get("/realtime", response_model=RealtimeSnapshot)
async def realtime(p: CurrentPrincipal, db: DB) -> dict:
    snap = analytics_agent.snapshot(str(p.org_id))
    # source de vérité pour les appels actifs : la base (multi-process)
    rows = (await db.execute(select(Call.direction, func.count()).where(
        Call.organization_id == p.org_id, Call.status == CallStatus.active.value).group_by(Call.direction))).all()
    by_dir = {"inbound": 0, "outbound": 0} | {d: int(n) for d, n in rows}
    cap = get_settings().global_max_concurrent_calls
    snap.update(active_calls=sum(by_dir.values()), active_by_direction=by_dir, utilization=round(sum(by_dir.values()) / cap, 3))
    return snap


@router.get("/campaigns/{campaign_id}", response_model=CampaignAnalytics)
async def campaign_analytics(campaign_id: uuid.UUID, p: CurrentPrincipal, db: DB) -> CampaignAnalytics:
    await get_owned(db, Campaign, campaign_id, p.org_id, "Campagne")
    contacts = int(await db.scalar(select(func.count()).select_from(Contact).where(Contact.campaign_id == campaign_id)) or 0)
    calls = (await db.execute(select(Call.id, Call.status, Call.outcome, Call.duration_seconds, Call.cost_estimate)
                              .where(Call.campaign_id == campaign_id))).all()
    outcomes = Counter(c.outcome or c.status for c in calls)
    human = [c for c in calls if c.duration_seconds > 0 and c.outcome not in ("voicemail", "no_answer")]
    successes = sum(1 for c in calls if c.outcome in SUCCESS)
    total_cost = sum(c.cost_estimate for c in calls)
    failures = Counter(c.outcome or c.status for c in calls if c.outcome not in SUCCESS)
    ids = [c.id for c in calls]
    objections: Counter[str] = Counter()
    if ids:
        for payload in (await db.scalars(select(CallEvent.payload).where(CallEvent.call_id.in_(ids), CallEvent.type == "call.completed"))).all():
            objections.update(o.strip().lower() for o in (payload or {}).get("objections", []) if o)
    recs: list[str] = []
    rate = successes / len(human) if human else 0.0
    if calls and outcomes.get("voicemail", 0) / len(calls) > 0.3:
        recs.append("Beaucoup de répondeurs : décalez la fenêtre d'appel (12h-14h ou 17h-19h).")
    if human and rate < 0.2:
        recs.append("Taux de succès faible : raccourcissez l'introduction et ajoutez les réponses aux objections fréquentes dans la base.")
    if objections:
        recs.append(f"Objection principale : « {objections.most_common(1)[0][0]} » — ajoutez une réponse dédiée au script.")
    if calls and outcomes.get("not_interested", 0) / len(calls) > 0.4:
        recs.append("Beaucoup de refus : revoyez le ciblage de la liste ou la proposition de valeur.")
    return CampaignAnalytics(
        campaign_id=campaign_id, contacts=contacts, calls_made=len(calls), human_answers=len(human),
        voicemails=outcomes.get("voicemail", 0), successes=successes, success_rate=round(rate, 3), total_cost=round(total_cost, 4),
        cost_per_success=round(total_cost / successes, 4) if successes else None,
        average_duration_s=round(sum(c.duration_seconds for c in human) / len(human), 1) if human else 0.0,
        outcomes=dict(outcomes), top_objections=[{"objection": k, "count": v} for k, v in objections.most_common(5)],
        failure_reasons=dict(failures), recommendations=recs,
    )


@router.get("/calls")
async def calls_timeseries(p: CurrentPrincipal, db: DB, days: int = Query(7, ge=1, le=90),
                           bucket: str = Query("hour", pattern="^(hour|day)$")) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (await db.execute(select(Call.created_at, Call.direction, Call.status, Call.outcome, Call.cost_estimate)
                             .where(Call.organization_id == p.org_id, Call.created_at >= since))).all()
    series: dict[str, dict] = {}
    for r in rows:
        ts = r.created_at.replace(minute=0, second=0, microsecond=0)
        if bucket == "day":
            ts = ts.replace(hour=0)
        b = series.setdefault(ts.isoformat(), {"t": ts.isoformat(), "inbound": 0, "outbound": 0, "success": 0, "failed": 0, "cost": 0.0})
        b[r.direction] += 1
        b["success"] += int(r.outcome == "success")
        b["failed"] += int(r.status == "failed")
        b["cost"] = round(b["cost"] + r.cost_estimate, 4)
    return {"bucket": bucket, "series": sorted(series.values(), key=lambda x: x["t"])}
