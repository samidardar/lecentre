"""Campagne sortante de bout en bout + charge : 100 appels simultanés (mock télécom + mock Gemini Live)."""
from __future__ import annotations

import asyncio
import time

import httpx

from app.core import events as ev
from app.services.call_manager import get_call_manager
from tests.test_api import wait_for


async def _campaign_with_contacts(client: httpx.AsyncClient, auth: dict[str, str], n: int, concurrency: int) -> str:
    camp = (await client.post("/api/v1/campaigns", headers=auth, json={
        "name": "Charge", "objective": "qualify_lead", "max_concurrency": concurrency, "max_attempts": 1,
        "script": "Présenter l'offre à {first_name}."})).json()
    rows = "\n".join(f"Client{i},+336{i:08d}" for i in range(n))
    csv_data = f"first_name,phone\n{rows}\n"
    r = await client.post(f"/api/v1/campaigns/{camp['id']}/contacts/import", headers=auth,
                          files={"file": ("c.csv", csv_data.encode(), "text/csv")})
    assert r.json()["imported"] == n
    return camp["id"]


async def test_campaign_runs_to_completion_with_events(client: httpx.AsyncClient, auth: dict[str, str]) -> None:
    received: list[str] = []

    async def listen() -> None:
        async for e in ev.get_bus().subscribe():
            received.append(e.event)

    listener = asyncio.create_task(listen())
    cid = await _campaign_with_contacts(client, auth, 6, 3)
    r = await client.post(f"/api/v1/campaigns/{cid}/start", headers=auth)
    assert r.status_code == 200 and r.json()["status"] == "running"

    async def finished() -> dict | None:
        p = (await client.get(f"/api/v1/campaigns/{cid}/progress", headers=auth)).json()
        return p if p["status"] == "completed" else None

    progress = await wait_for(finished, timeout=60)
    listener.cancel()
    assert progress["total_contacts"] == 6 and progress["percent"] == 100.0
    assert progress["completed"] + progress["failed"] + progress["opted_out"] == 6
    for name in (ev.CALL_STARTED, ev.CALL_ANSWERED, ev.CALL_TOOL_CALLED, ev.CAMPAIGN_PROGRESS):
        assert name in received, name
    calls = (await client.get(f"/api/v1/calls?campaign_id={cid}", headers=auth)).json()
    assert calls["total"] == 6
    stats = (await client.get(f"/api/v1/analytics/campaigns/{cid}", headers=auth)).json()
    assert stats["calls_made"] == 6 and stats["total_cost"] > 0


async def test_pause_and_resume(client: httpx.AsyncClient, auth: dict[str, str]) -> None:
    cid = await _campaign_with_contacts(client, auth, 10, 1)
    await client.post(f"/api/v1/campaigns/{cid}/start", headers=auth)
    paused = await client.post(f"/api/v1/campaigns/{cid}/pause", headers=auth)
    assert paused.json()["status"] == "paused"
    await asyncio.sleep(0.5)
    p = (await client.get(f"/api/v1/campaigns/{cid}/progress", headers=auth)).json()
    assert p["pending"] > 0  # la pause a stoppé l'enchaînement
    assert (await client.patch(f"/api/v1/campaigns/{cid}", headers=auth, json={"max_concurrency": 5})).status_code == 200
    await client.post(f"/api/v1/campaigns/{cid}/start", headers=auth)


async def test_100_concurrent_calls(client: httpx.AsyncClient, auth: dict[str, str]) -> None:
    """Critère MVP : 100 appels simultanés sans blocage ni erreur."""
    manager = get_call_manager()
    manager.telephony.think_time = 0.8  # type: ignore[attr-defined]  # appels qui durent → vraie concurrence
    cid = await _campaign_with_contacts(client, auth, 100, 100)
    peak = 0

    async def sample() -> None:  # échantillonnage direct, indépendant de la latence HTTP
        nonlocal peak
        while True:
            peak = max(peak, manager.local_active)
            await asyncio.sleep(0.005)

    sampler = asyncio.create_task(sample())
    t0 = time.perf_counter()
    await client.post(f"/api/v1/campaigns/{cid}/start", headers=auth)

    async def finished() -> dict | None:
        p = (await client.get(f"/api/v1/campaigns/{cid}/progress", headers=auth)).json()
        return p if p["status"] == "completed" else None

    progress = await wait_for(finished, timeout=120, interval=0.05)
    sampler.cancel()
    elapsed = time.perf_counter() - t0
    calls = (await client.get(f"/api/v1/calls?campaign_id={cid}&limit=500", headers=auth)).json()["items"]
    failed = [c for c in calls if c["status"] == "failed"]
    assert progress["total_contacts"] == 100 and not failed, failed[:3]
    assert peak >= 80, f"pic de concurrence trop faible: {peak}"
    assert elapsed < 60
