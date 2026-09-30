from __future__ import annotations

from app.core.config import get_settings
from app.live.base import LiveModel

_model: LiveModel | None = None


def get_live_model() -> LiveModel:
    global _model
    if _model is None:
        s = get_settings()
        if s.live_provider == "cascade":
            from app.live.cascade import CascadeLiveModel

            _model = CascadeLiveModel(s)
        elif s.live_provider == "gemini":
            from app.live.gemini import GeminiLiveModel

            _model = GeminiLiveModel(s)
        else:
            from app.live.mock import MockLiveModel

            _model = MockLiveModel()
    return _model


def set_live_model(model: LiveModel | None) -> None:
    global _model
    _model = model
