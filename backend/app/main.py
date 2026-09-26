from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.agents.analytics import analytics_agent
from app.agents.mcp_client import mcp_manager
from app.api import analytics, auth, calls, campaigns, knowledge, organizations, phone_numbers, telephony_webhooks, websocket
from app.core.config import get_settings
from app.core.events import get_bus
from app.core.logging import setup_logging
from app.db import create_all, dispose, get_engine
from app.services.call_manager import get_call_manager
from app.services.dispatcher import get_dispatcher

logger = logging.getLogger("callwiz")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    s = get_settings()
    setup_logging(s.log_level)
    if s.is_sqlite or s.environment != "prod":
        await create_all()
    get_bus()
    websocket.hub.start()
    analytics_task = asyncio.create_task(analytics_agent.run())
    await mcp_manager.start()
    manager, dispatcher = get_call_manager(), get_dispatcher()
    if app.state.run_dispatcher:
        await dispatcher.recover()
    from app.agents.graphs import get_graphs  # compile les graphes LangGraph au démarrage (pas au 1er appel)
    from app.rag.service import get_rag

    get_graphs()
    await asyncio.to_thread(get_rag)  # charge le modèle d'embedding hors boucle
    logger.info("CallWiz AI prêt", extra={"extra_fields": {"live": s.live_provider, "telephony": s.telephony_provider,
                                                            "db": s.database_url.split(":")[0], "vector_store": s.vector_store}})
    try:
        yield
    finally:
        await dispatcher.shutdown()
        await manager.shutdown()
        await analytics_agent.stop()
        analytics_task.cancel()
        await websocket.hub.stop()
        await mcp_manager.close()
        with contextlib.suppress(Exception):
            await get_rag().close()
        await dispose()


def _error(status: int, code: str, message: str, details: object = None) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message, "details": details}})


def create_app(*, run_dispatcher: bool = True) -> FastAPI:
    s = get_settings()
    app = FastAPI(
        title="CallWiz AI API", version="0.1.0", lifespan=lifespan,
        description="Centre d'appels IA : campagnes sortantes, standard entrant, RAG, analytics temps réel. "
                    "Événements temps réel : voir /ws/calls, /ws/campaigns/{id}, /ws/analytics.",
    )
    app.state.run_dispatcher = run_dispatcher
    app.add_middleware(CORSMiddleware, allow_origin_regex=s.cors_origin_regex, allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"], expose_headers=["X-Response-Time-ms"])

    @app.middleware("http")
    async def timing(request: Request, call_next):  # type: ignore[no-untyped-def]
        t0 = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Response-Time-ms"] = f"{(time.perf_counter() - t0) * 1000:.1f}"
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_exc(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        if isinstance(exc.detail, dict) and "code" in exc.detail:
            return _error(exc.status_code, exc.detail["code"], exc.detail["message"], exc.detail.get("details"))
        return _error(exc.status_code, "http_error", str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def validation_exc(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
        return _error(422, "validation_error", "Requête invalide", details)

    @app.exception_handler(Exception)
    async def unhandled(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("erreur non gérée")
        return _error(500, "internal_error", "Erreur interne")

    api = APIRouter()
    for module in (auth, organizations, campaigns, calls, phone_numbers, knowledge, analytics):
        api.include_router(module.router)
    app.include_router(api, prefix=s.api_prefix)
    if s.expose_unprefixed_routes:
        app.include_router(api, include_in_schema=False)
    app.include_router(websocket.router)
    app.include_router(telephony_webhooks.router)

    @app.get("/health", tags=["system"])
    async def health() -> dict:
        db_ok = True
        try:
            async with get_engine().connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception:
            db_ok = False
        manager = get_call_manager()
        return {"status": "ok" if db_ok else "degraded", "db": db_ok, "live_provider": s.live_provider,
                "telephony_provider": s.telephony_provider, "active_sessions": manager.local_active,
                "capacity": s.global_max_concurrent_calls}

    @app.get("/metrics", tags=["system"], include_in_schema=False)
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


app = create_app()
