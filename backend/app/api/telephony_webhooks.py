"""Webhooks Twilio (voix, statut, AMD) et WebSocket Media Streams. Non authentifiés par JWT : signés par Twilio."""
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Request, Response, WebSocket

from app.core.config import get_settings
from app.services.call_manager import get_call_manager
from app.telephony.twilio_provider import reject_twiml, stream_twiml, validate_signature
from app.voice.transport import TwilioMediaTransport

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/telephony/twilio", tags=["telephony"], include_in_schema=True)

MACHINE = {"machine_start", "machine_end_beep", "machine_end_silence", "machine_end_other", "fax"}


async def _form(request: Request) -> dict[str, str]:
    form = {k: str(v) for k, v in (await request.form()).items()}
    s = get_settings()
    if s.telephony_provider == "twilio" and s.twilio_validate_signature and s.twilio_auth_token:
        # URL publique telle que vue par Twilio (derrière proxy/tunnel)
        url = s.public_base_url.rstrip("/") + request.url.path + (f"?{request.url.query}" if request.url.query else "")
        if not validate_signature(s.twilio_auth_token, url, form, request.headers.get("X-Twilio-Signature", "")):
            logger.warning("signature Twilio invalide")
            raise PermissionError("invalid twilio signature")
    return form


def _xml(body: str) -> Response:
    return Response(content=body, media_type="application/xml")


@router.post("/voice")
async def voice(request: Request, call_id: str | None = None) -> Response:
    try:
        form = await _form(request)
    except PermissionError:
        return Response(status_code=403)
    manager = get_call_manager()
    if call_id is None:  # appel entrant
        call = await manager.create_inbound(form.get("To", ""), form.get("From", ""), form.get("CallSid"))
        if call is None:
            return _xml(reject_twiml("Ce numéro n'est pas attribué. Au revoir."))
        call_id = str(call.id)
    return _xml(stream_twiml(call_id))


@router.post("/status")
async def status(request: Request, call_id: str) -> Response:
    try:
        form = await _form(request)
    except PermissionError:
        return Response(status_code=403)
    st = form.get("CallStatus", "")
    manager = get_call_manager()
    cid = uuid.UUID(call_id)
    if st in {"no-answer", "busy", "failed", "canceled"}:
        await manager.mark_unanswered(call_id, st, error=form.get("ErrorMessage"))
    elif st == "completed":
        await manager.end_call(cid)
    return Response(status_code=204)


@router.post("/amd")
async def amd(request: Request, call_id: str) -> Response:
    try:
        form = await _form(request)
    except PermissionError:
        return Response(status_code=403)
    if form.get("AnsweredBy", "") in MACHINE:
        await get_call_manager().on_voicemail(uuid.UUID(call_id))
    return Response(status_code=204)


@router.websocket("/media")
async def media(ws: WebSocket) -> None:
    await ws.accept()
    transport = TwilioMediaTransport(ws)
    try:
        start = await transport.wait_start()
    except Exception as exc:
        logger.warning("stream Twilio invalide: %s", exc)
        await transport.close()
        return
    call_id = (start.get("customParameters") or {}).get("call_id")
    if not call_id:
        await transport.close()
        return
    await get_call_manager().start_session(uuid.UUID(call_id), transport, provider_call_id=transport.call_sid)
