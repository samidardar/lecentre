"""Twilio Programmable Voice : REST (appels, transfert, raccrochage) + TwiML Media Streams + validation de signature.

Flux :
  sortant  : POST Calls.json (Url=/telephony/twilio/voice?call_id=…, AMD asynchrone) → TwiML <Connect><Stream>
  entrant  : numéro Twilio → webhook /telephony/twilio/voice → création Call → TwiML <Connect><Stream>
  média    : WS /telephony/twilio/media (µ-law 8 kHz) → CallSession
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Mapping
from urllib.parse import urlencode
from xml.sax.saxutils import escape, quoteattr

import httpx

from app.core.config import Settings, get_settings
from app.core.resilience import CircuitBreaker, retry_async
from app.telephony.base import DialResult, TelephonyError


def compute_signature(auth_token: str, url: str, params: Mapping[str, str]) -> str:
    data = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    return base64.b64encode(hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()).decode()


def validate_signature(auth_token: str, url: str, params: Mapping[str, str], signature: str) -> bool:
    return hmac.compare_digest(compute_signature(auth_token, url, params), signature or "")


def ws_url(base: str) -> str:
    return base.replace("https://", "wss://").replace("http://", "ws://")


def stream_twiml(call_id: str, settings: Settings | None = None) -> str:
    s = settings or get_settings()
    url = f"{ws_url(s.public_base_url)}/telephony/twilio/media"
    return (
        '<?xml version="1.0" encoding="UTF-8"?><Response><Connect>'
        f"<Stream url={quoteattr(url)}><Parameter name=\"call_id\" value={quoteattr(call_id)} /></Stream>"
        "</Connect></Response>"
    )


def dial_twiml(number: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Dial answerOnBridge="true">{escape(number)}</Dial></Response>'


def reject_twiml(message: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Say language="fr-FR">{escape(message)}</Say><Hangup/></Response>'


class TwilioProvider:
    name = "twilio"

    def __init__(self, settings: Settings | None = None) -> None:
        self.s = settings or get_settings()
        if not (self.s.twilio_account_sid and self.s.twilio_auth_token):
            raise TelephonyError("TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN manquants")
        self._base = f"https://api.twilio.com/2010-04-01/Accounts/{self.s.twilio_account_sid}"
        self._http = httpx.AsyncClient(auth=(self.s.twilio_account_sid, self.s.twilio_auth_token), timeout=10,
                                       limits=httpx.Limits(max_connections=50, max_keepalive_connections=20))
        self._breaker = CircuitBreaker("twilio", threshold=8, reset_after=20)

    async def _post(self, path: str, data: dict[str, str]) -> dict:
        async def go() -> dict:
            r = await self._http.post(f"{self._base}{path}", data=data)
            if r.status_code >= 500 or r.status_code == 429:
                raise TelephonyError(f"twilio {r.status_code}")
            if r.status_code >= 400:
                raise ValueError(f"twilio {r.status_code}: {r.text[:300]}")
            return r.json()

        return await self._breaker.call(lambda: retry_async(go, attempts=3, retry_on=(TelephonyError, httpx.TransportError)))

    async def make_call(self, call_id: str, to: str, from_: str, *, detect_voicemail: bool = True) -> DialResult:
        base = self.s.public_base_url
        q = urlencode({"call_id": call_id})
        data = {
            "To": to, "From": from_,
            "Url": f"{base}/telephony/twilio/voice?{q}",
            "StatusCallback": f"{base}/telephony/twilio/status?{q}",
            "StatusCallbackEvent": "initiated ringing answered completed",
            "Timeout": "25",
        }
        if detect_voicemail:
            data |= {
                "MachineDetection": "DetectMessageEnd", "AsyncAmd": "true",
                "AsyncAmdStatusCallback": f"{base}/telephony/twilio/amd?{q}",
            }
        res = await self._post("/Calls.json", data)
        return DialResult(provider_call_id=res["sid"], status=res.get("status", "queued"))

    async def hangup(self, provider_call_id: str) -> None:
        await self._post(f"/Calls/{provider_call_id}.json", {"Status": "completed"})

    async def transfer(self, provider_call_id: str, to: str) -> None:
        await self._post(f"/Calls/{provider_call_id}.json", {"Twiml": dial_twiml(to)})

    async def close(self) -> None:
        await self._http.aclose()
