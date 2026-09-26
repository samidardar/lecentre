from __future__ import annotations

import asyncio
import audioop
import base64
import json

from app.live.mock import MockLiveModel
from app.live.base import AudioOut, Interrupted, LiveConfig, ToolDeclaration, TurnComplete
from app.telephony.twilio_provider import compute_signature, stream_twiml, validate_signature
from app.voice.audio import EnergyVAD, Resampler, pcm16_to_ulaw, ulaw_to_pcm16
from app.voice.transport import TwilioMediaTransport


def test_twilio_signature_roundtrip() -> None:
    params = {"CallSid": "CA123", "From": "+33600000000", "To": "+33100000000"}
    url = "https://example.com/telephony/twilio/voice"
    sig = compute_signature("token", url, params)
    assert validate_signature("token", url, params, sig)
    assert not validate_signature("token", url, params | {"To": "+1"}, sig)


def test_stream_twiml_contains_call_id() -> None:
    xml = stream_twiml("abc-123")
    assert "<Connect><Stream" in xml and 'value="abc-123"' in xml and "/telephony/twilio/media" in xml


def test_audio_conversion_and_vad() -> None:
    import math

    tone = b"".join(int(8000 * math.sin(i / 5)).to_bytes(2, "little", signed=True) for i in range(160))
    ulaw = pcm16_to_ulaw(tone)
    assert len(ulaw) == 160 and len(ulaw_to_pcm16(ulaw)) == 320
    up = Resampler(8000, 16000)(tone)
    assert abs(len(up) - 640) <= 4
    vad = EnergyVAD(threshold=500, hangover_frames=2)
    assert vad.update(tone) == (True, False)
    silence = b"\x00\x00" * 160
    vad.update(silence)
    assert vad.update(silence) == (False, True)


class FakeWS:
    def __init__(self, incoming: list[dict]) -> None:
        self.incoming = [json.dumps(m) for m in incoming]
        self.sent: list[dict] = []

    async def receive_text(self) -> str:
        if not self.incoming:
            await asyncio.sleep(10)
        return self.incoming.pop(0)

    async def send_text(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def close(self) -> None:
        pass


async def test_twilio_media_transport() -> None:
    payload = base64.b64encode(b"\xff" * 160).decode()
    ws = FakeWS([
        {"event": "connected"},
        {"event": "start", "start": {"streamSid": "MZ1", "callSid": "CA1", "customParameters": {"call_id": "x"}}},
        *[{"event": "media", "media": {"track": "inbound", "payload": payload}} for _ in range(4)],
        {"event": "stop"},
    ])
    t = TwilioMediaTransport(ws)  # type: ignore[arg-type]
    start = await t.wait_start()
    assert start["customParameters"]["call_id"] == "x"
    kinds = [m.kind async for m in t.receive()]
    assert kinds.count("audio") == 2 and kinds[-1] == "stop"
    await t.send_audio(b"\x00\x00" * 2400)  # 100 ms @ 24 kHz
    await t.clear()
    assert ws.sent[0]["event"] == "media" and len(base64.b64decode(ws.sent[0]["media"]["payload"])) == 800
    assert ws.sent[1] == {"event": "clear", "streamSid": "MZ1"}


async def test_mock_live_barge_in() -> None:
    model = MockLiveModel(first_audio_delay=0.0, realtime_factor=1.0)
    session = await model.connect(LiveConfig("sys", tools=[ToolDeclaration("end_call", "", {})], greeting_hint="Bonjour " * 30))
    await session.send_text("[APPEL_DEBUT]")
    events = session.events()
    first = await events.__anext__()
    while not isinstance(first, AudioOut):
        first = await events.__anext__()
    loud = audioop.mul(b"\x00\x10" * 320, 2, 4)
    await session.send_audio(loud)
    seen = []
    while True:
        e = await asyncio.wait_for(events.__anext__(), 2)
        seen.append(type(e))
        if isinstance(e, Interrupted):
            break
    assert TurnComplete not in seen
    await session.close()
