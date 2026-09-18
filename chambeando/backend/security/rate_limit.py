"""
Abstraccion de rate limiting. `RateLimiter` es el puerto; `InMemoryRateLimiter`
es la unica implementacion en esta fase, explicitamente marcada como SOLO para
dev/tests locales — no es apta para produccion multi-proceso/multi-worker
porque su estado vive en memoria del proceso Python (se pierde en cada reinicio
y no se comparte entre workers de uvicorn/gunicorn). Produccion debe reemplazar
esto por Redis (`INCR` + `EXPIRE`) o limitacion a nivel de gateway/API — ese
reemplazo es un archivo nuevo que implemente `RateLimiter`, no un cambio a los
callers (misma razon de ser que EscrowChainAdapter para la cadena).
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections import defaultdict, deque

from fastapi import HTTPException


class RateLimiter(ABC):
    @abstractmethod
    def allow(self, key: str, *, limit: int, window_seconds: int) -> bool:
        """True si `key` todavia tiene cupo dentro de la ventana; si es True,
        tambien registra este intento contra el limite."""


class InMemoryRateLimiter(RateLimiter):
    """Ventana deslizante simple: por `key`, una deque de timestamps de intentos
    recientes. SOLO dev/tests — ver docstring del modulo."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, *, limit: int, window_seconds: int) -> bool:
        now = time.monotonic()
        hits = self._hits[key]
        cutoff = now - window_seconds
        while hits and hits[0] < cutoff:
            hits.popleft()
        if len(hits) >= limit:
            return False
        hits.append(now)
        return True

    def reset_for_tests(self) -> None:
        self._hits.clear()


_limiter = InMemoryRateLimiter()


def get_rate_limiter() -> RateLimiter:
    return _limiter


def enforce_rate_limit(limiter: RateLimiter, key: str, limit: int, window_seconds: int = 60) -> None:
    """Dependency-friendly helper: 429 si `key` ya agoto su cupo en la ventana."""
    if not limiter.allow(key, limit=limit, window_seconds=window_seconds):
        raise HTTPException(status_code=429, detail="Demasiados intentos — probá de nuevo en un momento")


def reset_rate_limiter_for_tests() -> None:
    _limiter.reset_for_tests()
