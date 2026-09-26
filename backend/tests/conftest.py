from __future__ import annotations

import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="callwiz-test-")
os.environ.update({
    "ENVIRONMENT": "test",
    "DATABASE_URL": f"sqlite+aiosqlite:///{_tmp}/test.db",
    "DATA_DIR": _tmp,
    "LIVE_PROVIDER": "mock",
    "TELEPHONY_PROVIDER": "mock",
    "VECTOR_STORE": "memory",
    "EMBEDDER": "hashing",
    "REDIS_URL": "",
    "LOG_LEVEL": "WARNING",
    "TELEPHONY_CALLS_PER_SECOND": "500",
    "SILENCE_NUDGE_S": "30",
    "SILENCE_CLOSE_S": "60",
    "SILENCE_HANGUP_S": "90",
})

from typing import AsyncIterator  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402

from app.agents.graphs import get_graphs  # noqa: E402
from app.core import events  # noqa: E402
from app.db import create_all, dispose  # noqa: E402
from app.live import set_live_model  # noqa: E402
from app.live.mock import MockLiveModel  # noqa: E402
from app.llm.text import HeuristicTextLLM, set_text_llm  # noqa: E402
from app.rag.embedding import HashingEmbedder  # noqa: E402
from app.rag.service import RagService, set_rag  # noqa: E402
from app.rag.store import MemoryVectorStore  # noqa: E402
from app.services import call_manager as cm  # noqa: E402
from app.services import dispatcher as dp  # noqa: E402
from app.telephony.mock_provider import MockTelephonyProvider  # noqa: E402

FAQ = """# Informations pratiques

Q: Quels sont vos horaires d'ouverture ?
R: Nous sommes ouverts du lundi au vendredi de 9h à 18h, et le samedi de 10h à 13h.

Q: Quelle est votre politique de retour ?
R: Les retours sont acceptés sous 30 jours avec le ticket de caisse. Le remboursement est effectué sous 5 jours ouvrés.

# Tarifs

Q: Combien coûte l'abonnement premium ?
R: L'abonnement premium coûte 29 euros par mois, sans engagement.
"""


@pytest.fixture(autouse=True)
async def _env() -> AsyncIterator[None]:
    await dispose()
    from app.db import get_engine
    from app.models import Base

    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await create_all()
    events.set_bus(events.MemoryBus())
    set_live_model(MockLiveModel(first_audio_delay=0.02))
    set_text_llm(HeuristicTextLLM())
    set_rag(RagService(embedder=HashingEmbedder(), store=MemoryVectorStore()))
    telephony = MockTelephonyProvider(ring_delay=0.01, think_time=0.01)
    manager = cm.CallManager(telephony)
    cm.set_call_manager(manager)
    dispatcher = dp.CampaignDispatcher(manager, respect_schedule=False)
    dispatcher.poll_interval_s = 0.05
    dp.set_dispatcher(dispatcher)
    get_graphs()
    yield
    await dispatcher.shutdown()
    await manager.shutdown()
    await dispose()


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    from app.agents.analytics import analytics_agent
    from app.api.websocket import hub
    import asyncio

    from app.main import create_app

    app = create_app(run_dispatcher=False)
    hub.start()
    task = asyncio.create_task(analytics_agent.run())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await analytics_agent.stop()
    task.cancel()
    await hub.stop()


@pytest.fixture
async def auth(client: httpx.AsyncClient) -> dict[str, str]:
    r = await client.post("/api/v1/auth/register", json={"email": "owner@acme.example.com", "password": "s3cret-pass", "organization_name": "Acme"})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}
