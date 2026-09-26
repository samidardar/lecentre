"""Conversions audio temps réel (C natif via audioop, ~µs par frame)."""
from __future__ import annotations

import audioop


class Resampler:
    """Rééchantillonneur à état (continuité entre frames, pas de clics)."""

    def __init__(self, in_rate: int, out_rate: int) -> None:
        self.in_rate, self.out_rate = in_rate, out_rate
        self._state = None

    def __call__(self, pcm16: bytes) -> bytes:
        if self.in_rate == self.out_rate or not pcm16:
            return pcm16
        out, self._state = audioop.ratecv(pcm16, 2, 1, self.in_rate, self.out_rate, self._state)
        return out


def ulaw_to_pcm16(data: bytes) -> bytes:
    return audioop.ulaw2lin(data, 2)


def pcm16_to_ulaw(data: bytes) -> bytes:
    return audioop.lin2ulaw(data, 2)


def rms(pcm16: bytes) -> int:
    return audioop.rms(pcm16, 2) if pcm16 else 0


class EnergyVAD:
    """VAD énergétique léger : sert uniquement aux métriques de latence et à la détection des silences
    (le turn-taking réel est assuré par le VAD natif de Gemini Live)."""

    def __init__(self, threshold: int = 700, hangover_frames: int = 8) -> None:
        self.threshold = threshold
        self.hangover_frames = hangover_frames
        self._below = hangover_frames
        self.speaking = False

    def update(self, pcm16: bytes) -> tuple[bool, bool]:
        """Retourne (speech_started, speech_ended) pour cette frame."""
        loud = rms(pcm16) >= self.threshold
        started = ended = False
        if loud:
            self._below = 0
            if not self.speaking:
                self.speaking, started = True, True
        else:
            self._below += 1
            if self.speaking and self._below >= self.hangover_frames:
                self.speaking, ended = False, True
        return started, ended
