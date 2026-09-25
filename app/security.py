"""Validação de webhooks/tokens, rate limit por telefone e máscara de telefones nos logs."""

from __future__ import annotations

import hmac
import logging
import re
import time
from collections import defaultdict, deque

from fastapi import Header, HTTPException, Query

from app.config import get_settings

_PHONE_RE = re.compile(r"\+?\d[\d\s().-]{7,}\d")


def mask_phone(phone: str | None) -> str:
    """+5516999998888 -> +55*******8888"""
    if not phone:
        return "?"
    digits = re.sub(r"\D", "", phone)
    if len(digits) <= 4:
        return "****"
    return f"+{digits[:2]}{'*' * (len(digits) - 6)}{digits[-4:]}"


class PhoneMaskingFilter(logging.Filter):
    """Mascara qualquer número de telefone que apareça numa linha de log."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        masked = _PHONE_RE.sub(lambda m: mask_phone(m.group(0)), msg)
        if masked != msg:
            record.msg, record.args = masked, None
        return True


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(PhoneMaskingFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


def normalize_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    return f"+{digits}" if digits else None


def _safe_equal(a: str, b: str) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a.encode(), b.encode())


def verify_webhook_token(
    token: str | None = Query(default=None),
    x_webhook_token: str | None = Header(default=None),
) -> None:
    """O Agent Bot do Chatwoot chama https://<bot>/webhooks/chatwoot?token=<WEBHOOK_SECRET>."""
    expected = get_settings().webhook_secret
    if not _safe_equal(token or x_webhook_token or "", expected):
        raise HTTPException(status_code=401, detail="invalid token")


def verify_scheduler_token(authorization: str | None = Header(default=None)) -> None:
    expected = get_settings().scheduler_token
    provided = (authorization or "").removeprefix("Bearer ").strip()
    if not _safe_equal(provided, expected):
        raise HTTPException(status_code=401, detail="invalid token")


class RateLimiter:
    """Janela deslizante em memória (o bot roda em uma única instância)."""

    def __init__(self, limit: int, period: float = 60.0) -> None:
        self.limit = limit
        self.period = period
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        hits = self._hits[key]
        while hits and now - hits[0] > self.period:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True
