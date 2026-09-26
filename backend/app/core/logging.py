from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
from datetime import datetime, timezone
from typing import Any

call_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("call_id", default=None)
org_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("organization_id", default=None)

_REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)(authorization|api[_-]?key|token|password|secret)([\"']?\s*[:=]\s*[\"']?)[^\s\"',}]+"), r"\1\2[REDACTED]"),
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "[CARD]"),
    (re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}\b"), "[IBAN]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[EMAIL]"),
    (re.compile(r"(\+?\d{2})\d{5,9}(\d{2})\b"), r"\1*****\2"),
]


def redact(text: str) -> str:
    for pattern, repl in _REDACTIONS:
        text = pattern.sub(repl, text)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        if cid := call_id_var.get():
            payload["call_id"] = cid
        if oid := org_id_var.get():
            payload["organization_id"] = oid
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload.update({k: redact(v) if isinstance(v, str) else v for k, v in extra.items()})
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for noisy in ("httpx", "httpcore", "websockets", "aiosqlite", "google_genai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def log(logger: logging.Logger, level: int, msg: str, **fields: Any) -> None:
    logger.log(level, msg, extra={"extra_fields": fields})
