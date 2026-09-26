from __future__ import annotations

import asyncio

import httpx

from tests.conftest import FAQ


async def wait_for(predicate, timeout: float = 10.0, interval: float = 0.05):  # type: ignore[no-untyped-def]
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        result = await predicate()
        if result:
            return result
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timeout en attente d'une condition")
        await asyncio.sleep(interval)


async def test_health(client: httpx.AsyncClient) -> None:
    r = await client.get("/health")
    assert r.status_code == 200 and r.json()["db"] is True


async def test_auth_flow(client: httpx.AsyncClient, auth: dict[str, str]) -> None:
    me = await client.get("/api/v1/auth/me", headers=auth)
    assert me.status_code == 200 and me.json()["email"] == "owner@acme.example.com"
    login = await client.post("/api/v1/auth/login", json={"email": "owner@acme.example.com", "password": "s3cret-pass"})
    assert login.status_code == 200
    bad = await client.post("/api/v1/auth/login", json={"email": "owner@acme.example.com", "password": "wrong-pass"})
    assert bad.status_code == 401 and bad.json()["error"]["code"] == "invalid_credentials"
    ref = await client.post("/api/v1/auth/refresh", json={"refresh_token": login.json()["refresh_token"]})
    assert ref.status_code == 200
    # alias sans préfixe pour le frontend
    assert (await client.get("/auth/me", headers=auth)).status_code == 200
    assert (await client.get("/api/v1/auth/me")).json()["error"]["code"] == "not_authenticated"


async def test_org_update(client: httpx.AsyncClient, auth: dict[str, str]) -> None:
    r = await client.patch("/api/v1/organizations/me", headers=auth, json={"tone": "chaleureux", "business_description": "Magasin de vélos"})
    assert r.status_code == 200 and r.json()["tone"] == "chaleureux"


async def test_tenant_isolation(client: httpx.AsyncClient, auth: dict[str, str]) -> None:
    camp = (await client.post("/api/v1/campaigns", headers=auth, json={"name": "A"})).json()
    other = await client.post("/api/v1/auth/register", json={"email": "x@other.example.com", "password": "another-pass", "organization_name": "Other"})
    h2 = {"Authorization": f"Bearer {other.json()['access_token']}"}
    assert (await client.get(f"/api/v1/campaigns/{camp['id']}", headers=h2)).status_code == 404
    assert (await client.get("/api/v1/campaigns", headers=h2)).json()["total"] == 0


async def test_campaign_crud_and_csv_import(client: httpx.AsyncClient, auth: dict[str, str]) -> None:
    r = await client.post("/api/v1/campaigns", headers=auth, json={
        "name": "Relance devis", "objective": "qualify_lead", "script": "Bonjour {first_name}", "max_concurrency": 5})
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    csv_data = "Prénom;Nom;Téléphone;Ville\nAlice;Martin;06 12 34 56 78;Paris\nBob;Durand;+33698765432;Lyon\nBad;Row;123;X\nAlice;Martin;0612345678;Paris\n"
    imp = await client.post(f"/api/v1/campaigns/{cid}/contacts/import", headers=auth,
                            files={"file": ("contacts.csv", csv_data.encode(), "text/csv")})
    body = imp.json()
    assert imp.status_code == 200, imp.text
    assert body["imported"] == 2 and body["skipped"] == 2 and body["mapping_used"]["phone"] == "Téléphone"
    contacts = (await client.get(f"/api/v1/campaigns/{cid}/contacts", headers=auth)).json()
    assert contacts["total"] == 2 and contacts["items"][0]["attributes"]["Ville"] == "Paris"
    assert contacts["items"][0]["phone"] == "+33612345678"
    upd = await client.patch(f"/api/v1/campaigns/{cid}", headers=auth, json={"max_concurrency": 10})
    assert upd.json()["max_concurrency"] == 10
    err = await client.post("/api/v1/campaigns", headers=auth, json={"name": "x", "max_concurrency": 1000})
    assert err.status_code == 422 and err.json()["error"]["code"] == "validation_error"


async def test_knowledge_ingest_and_query(client: httpx.AsyncClient, auth: dict[str, str]) -> None:
    kb = (await client.post("/api/v1/knowledge-bases", headers=auth, json={"name": "FAQ"})).json()
    doc = await client.post(f"/api/v1/knowledge-bases/{kb['id']}/documents", headers=auth, files={"file": ("faq.md", FAQ.encode(), "text/markdown")})
    assert doc.status_code == 202
    doc_id = doc.json()["id"]

    async def ready() -> bool:
        st = (await client.get(f"/api/v1/documents/{doc_id}/status", headers=auth)).json()
        assert st["status"] != "failed", st
        return st["status"] == "ready"

    await wait_for(ready)
    q = await client.post(f"/api/v1/knowledge-bases/{kb['id']}/test-query", headers=auth, json={"query": "Quelle est votre politique de retour ?"})
    res = q.json()
    assert q.status_code == 200 and not res["low_confidence"]
    assert "30 jours" in res["chunks"][0]["content"]
    off = (await client.post(f"/api/v1/knowledge-bases/{kb['id']}/test-query", headers=auth, json={"query": "zebulon quantique xylophone"})).json()
    assert off["low_confidence"] is True
    assert (await client.delete(f"/api/v1/documents/{doc_id}", headers=auth)).status_code == 204


async def test_phone_numbers(client: httpx.AsyncClient, auth: dict[str, str]) -> None:
    r = await client.post("/api/v1/phone-numbers", headers=auth, json={"number": "01 23 45 67 89", "greeting": "Bonjour Acme"})
    assert r.status_code == 201 and r.json()["number"] == "+33123456789"
    assert (await client.post("/api/v1/phone-numbers", headers=auth, json={"number": "+33123456789"})).status_code == 409
    nid = r.json()["id"]
    assert (await client.patch(f"/api/v1/phone-numbers/{nid}", headers=auth, json={"active": False})).json()["active"] is False


async def test_simulated_inbound_call_end_to_end(client: httpx.AsyncClient, auth: dict[str, str]) -> None:
    kb = (await client.post("/api/v1/knowledge-bases", headers=auth, json={"name": "FAQ"})).json()
    doc = (await client.post(f"/api/v1/knowledge-bases/{kb['id']}/documents/text", headers=auth, json={"filename": "faq.md", "content": FAQ})).json()

    async def ready() -> bool:
        return (await client.get(f"/api/v1/documents/{doc['id']}/status", headers=auth)).json()["status"] == "ready"

    await wait_for(ready)
    pn = (await client.post("/api/v1/phone-numbers", headers=auth, json={"number": "+33123456789", "knowledge_base_id": kb["id"]})).json()
    r = await client.post("/api/v1/calls/simulate", headers=auth, json={
        "direction": "inbound", "phone_number_id": pn["id"],
        "caller_script": ["Quelle est votre politique de retour ?", "Merci, au revoir."]})
    assert r.status_code == 202, r.text
    call_id = r.json()["id"]

    async def done() -> dict | None:
        c = (await client.get(f"/api/v1/calls/{call_id}", headers=auth)).json()
        return c if c["status"] in ("completed", "failed") else None

    call = await wait_for(done, timeout=20)
    assert call["status"] == "completed", call
    texts = " ".join(t["text"] for t in call["transcript"])
    assert "30 jours" in texts  # réponse issue du RAG
    assert call["transcript"][0]["role"] == "assistant" and "assistant virtuel" in call["transcript"][0]["text"]
    assert call["latency"]["avg_ms"] is not None and call["cost_estimate"] > 0

    async def debug_ready() -> dict | None:
        d = (await client.get(f"/api/v1/calls/{call_id}/debug", headers=auth)).json()
        return d if d["rag_chunks"] and d["decisions"] else None

    debug = await wait_for(debug_ready)
    assert any(dd["decision"] == "route" for dd in debug["decisions"])
    ov = (await client.get("/api/v1/analytics/overview", headers=auth)).json()
    assert ov["total_calls"] == 1 and ov["completed_calls"] == 1


async def test_voices_and_french_defaults(client: httpx.AsyncClient, auth: dict[str, str]) -> None:
    r = (await client.get("/api/v1/organizations/voices", headers=auth)).json()
    assert r["default_language"] == "fr" and r["default_voice"] == "Sulafat"
    assert len(r["voices"]) == 30 and r["voices"][0]["recommended_fr"] is True
    ok = await client.patch("/api/v1/organizations/me", headers=auth, json={"default_voice_id": "achird", "default_language": "fr"})
    assert ok.json()["default_voice_id"] == "Achird"
    bad = await client.patch("/api/v1/organizations/me", headers=auth, json={"default_voice_id": "Robot"})
    assert bad.status_code == 422
    assert (await client.post("/api/v1/campaigns", headers=auth, json={"name": "x", "language": "de"})).status_code == 422
