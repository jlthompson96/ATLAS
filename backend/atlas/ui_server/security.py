"""Shared security helpers for atlas-ui: response headers, Origin/Host policy and failure rate limiting."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Final
from urllib.parse import urlsplit

#: Same policy as the ``<meta>`` tag the HUD build injects (frontend/hud/vite.config.ts), plus the directives
#: only a header can carry. PLAN.md section 2.14: ``default-src 'self'``, no CDN, no remote images.
CSP: Final = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self'",
        "font-src 'self'",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
    ]
)

SECURITY_HEADERS: Final[dict[str, str]] = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    "Strict-Transport-Security": "max-age=31536000",
}


def normalize_origin(origin: str) -> str:
    return origin.strip().rstrip("/").lower()


def default_origins(port: int, extra: Iterable[str] = ()) -> frozenset[str]:
    """Origins the HUD may be loaded from: loopback names plus any configured LAN names (e.g. atlas.lan)."""
    suffix = "" if port == 443 else f":{port}"
    base = {f"https://localhost{suffix}", f"https://127.0.0.1{suffix}", f"https://[::1]{suffix}"}
    return frozenset(base | {normalize_origin(o) for o in extra})


def origin_allowed(origin: str | None, allowed: frozenset[str]) -> bool:
    """A browser WebSocket always sends Origin; a missing or ``null`` Origin is refused."""
    return origin is not None and normalize_origin(origin) in allowed


def same_origin_request(origin: str | None, sec_fetch_site: str | None, allowed: frozenset[str]) -> bool:
    """An HTTP request from a page of this server (not for WebSockets, which always carry an Origin).

    A browser leaves ``Origin`` out of a same-origin GET (Fetch standard), so the pairing page's own request has none;
    the browser then says ``Sec-Fetch-Site: same-origin``, a header page scripts can't set. A request from another
    site carries its foreign Origin (refused) and ``Sec-Fetch-Site: cross-site``. A request with neither header (not a
    browser page) is refused too. The Host header is checked separately, for every request.
    """
    if origin is not None:
        return origin_allowed(origin, allowed)
    return (sec_fetch_site or "").strip().lower() == "same-origin"


def host_allowed(host: str | None, allowed: frozenset[str]) -> bool:
    """The Host header must name this server (defence against DNS rebinding)."""
    if not host:
        return False
    return any(urlsplit(o).netloc == host.lower() for o in allowed)


@dataclass
class FailureLimiter:
    """Counts failures per key; ``max_failures`` within ``window_s`` locks the key for ``lockout_s``."""

    max_failures: int
    window_s: float
    lockout_s: float
    clock: Callable[[], float] = time.monotonic
    _failures: dict[str, list[float]] = field(default_factory=dict)
    _locked_until: dict[str, float] = field(default_factory=dict)

    def locked(self, key: str) -> bool:
        until = self._locked_until.get(key)
        if until is None:
            return False
        if self.clock() >= until:
            del self._locked_until[key]
            self._failures.pop(key, None)
            return False
        return True

    def failure(self, key: str) -> None:
        now = self.clock()
        recent = [t for t in self._failures.get(key, []) if now - t < self.window_s]
        recent.append(now)
        self._failures[key] = recent
        if len(recent) >= self.max_failures:
            self._locked_until[key] = now + self.lockout_s
        if len(self._failures) > 1024:  # bound memory under a spray of distinct keys
            self._failures = {k: v for k, v in self._failures.items() if v and now - v[-1] < self.window_s}

    def success(self, key: str) -> None:
        self._failures.pop(key, None)
