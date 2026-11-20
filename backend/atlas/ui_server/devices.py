"""Device pairing and device tokens (PLAN.md section 4.5, threat T7; contracts/services.md section 4).

* A **pairing code** is created out of band (``python -m atlas.ui_server pair``: shown on the kiosk or CLI),
  is single use, expires in minutes, and is stored only as a SHA-256 hash.
* Redeeming it registers a **device** (``kiosk`` or ``remote``) and issues a **device token**:
  ``<device_id>.<mac>`` where ``mac = HMAC-SHA256(device-token-key, "atlas-device-v1|" id "|" nonce)``. The nonce
  lives in the registry, so a token is revoked by revoking (or re-pairing) the device, and a stolen key alone
  can't mint a token for an unknown device.
* The registry and the pending codes are small JSON files in atlas-ui's state directory (0600, written
  atomically), re-read when they change, so the CLI can add codes and revoke devices while the server runs.

The role (and so the permissions) always comes from the registry, never from the token.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

Role = Literal["kiosk", "remote"]
ROLES: Final = ("kiosk", "remote")
CODE_ALPHABET: Final = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I
CODE_LENGTH: Final = 8
CODE_TTL_S: Final = 10 * 60
MAX_PENDING_CODES: Final = 8
MAX_DEVICES: Final = 32
COOKIE_NAME: Final = "__Host-atlas-device"
COOKIE_MAX_AGE_S: Final = 365 * 24 * 3600
_TOKEN_DOMAIN: Final = b"atlas-device-v1|"


@dataclass(frozen=True, slots=True)
class Device:
    device_id: str
    role: Role
    created_at: int
    label: str = ""

    @property
    def can_approve(self) -> bool:
        return self.role == "remote"  # the kiosk is view-only plus PTT and text (AP-4)

    @property
    def can_unlock_portfolio(self) -> bool:
        return self.role == "remote"


def _write_private(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        Path(tmp).chmod(0o600)
        Path(tmp).replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            Path(tmp).unlink()
        raise


class _JsonFile:
    """A JSON object file re-read when its mtime changes (so a CLI in another process is seen)."""

    def __init__(self, path: Path, empty: dict[str, Any]) -> None:
        self.path = path
        self._empty = empty
        self._stamp: tuple[int, int] | None = None
        self._data: dict[str, Any] = json.loads(json.dumps(empty))

    def read(self) -> dict[str, Any]:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            self._stamp, self._data = None, json.loads(json.dumps(self._empty))
            return self._data
        stamp = (st.st_mtime_ns, st.st_size)
        if stamp != self._stamp:
            with self.path.open(encoding="utf-8") as f:
                self._data = json.load(f)
            self._stamp = stamp
        return self._data

    def write(self, data: dict[str, Any]) -> None:
        _write_private(self.path, data)
        self._data = data
        st = self.path.stat()
        self._stamp = (st.st_mtime_ns, st.st_size)


class DeviceRegistry:
    def __init__(
        self,
        state_dir: Path,
        key: bytes,
        *,
        wall_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
    ) -> None:
        if len(key) < 32:
            raise ValueError("device-token-key must be at least 32 bytes")
        self._key = key
        self._wall = wall_ms
        self._devices = _JsonFile(state_dir / "devices.json", {"devices": {}})

    # --- tokens ---

    def _mac(self, device_id: str, nonce: str) -> str:
        digest = hmac.new(self._key, _TOKEN_DOMAIN + f"{device_id}|{nonce}".encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode().rstrip("=")

    def authenticate(self, token: str | None) -> Device | None:
        """The device a token belongs to, or ``None`` (malformed, forged, unknown or revoked)."""
        if not token or len(token) > 200 or token.count(".") != 1:
            return None
        device_id, mac = token.split(".")
        entry = self._devices.read()["devices"].get(device_id)
        if entry is None or entry.get("revoked"):
            return None
        if not hmac.compare_digest(self._mac(device_id, entry["nonce"]), mac):
            return None
        return Device(device_id, entry["role"], entry["created_at"], entry.get("label", ""))

    def authenticate_cookie(self, cookie_header: str | None) -> Device | None:
        return self.authenticate(parse_cookie(cookie_header, COOKIE_NAME))

    # --- management ---

    def add(self, role: Role, label: str = "") -> tuple[Device, str]:
        if role not in ROLES:
            raise ValueError("unknown role")
        data = self._devices.read()
        live = [d for d in data["devices"].values() if not d.get("revoked")]
        if len(live) >= MAX_DEVICES:
            raise OverflowError("too many paired devices; revoke one first")
        device_id = "dev-" + secrets.token_hex(6)
        nonce = secrets.token_hex(16)
        now = self._wall()
        data = {
            "devices": {
                **data["devices"],
                device_id: {"role": role, "nonce": nonce, "created_at": now, "label": label[:40], "revoked": False},
            }
        }
        self._devices.write(data)
        return Device(device_id, role, now, label[:40]), f"{device_id}.{self._mac(device_id, nonce)}"

    def revoke(self, device_id: str) -> bool:
        data = self._devices.read()
        entry = data["devices"].get(device_id)
        if entry is None or entry.get("revoked"):
            return False
        self._devices.write({"devices": {**data["devices"], device_id: {**entry, "revoked": True}}})
        return True

    def is_live(self, device_id: str) -> bool:
        entry = self._devices.read()["devices"].get(device_id)
        return entry is not None and not entry.get("revoked")

    def list(self) -> list[tuple[Device, bool]]:
        out = []
        for device_id, e in self._devices.read()["devices"].items():
            out.append((Device(device_id, e["role"], e["created_at"], e.get("label", "")), bool(e.get("revoked"))))
        return out


def parse_cookie(header: str | None, name: str) -> str | None:
    if not header or len(header) > 4096:
        return None
    for part in header.split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return None


def set_cookie_header(token: str) -> str:
    # __Host- prefix: Secure, Path=/, no Domain. HttpOnly + SameSite=Strict per PLAN.md section 4.5.
    return f"{COOKIE_NAME}={token}; Path=/; Max-Age={COOKIE_MAX_AGE_S}; Secure; HttpOnly; SameSite=Strict"


def _normalize_code(code: str) -> str:
    return "".join(c for c in code.upper() if c.isalnum())


def _code_hash(code: str) -> str:
    return hashlib.sha256(b"atlas-pairing-v1|" + _normalize_code(code).encode()).hexdigest()


class PairingStore:
    def __init__(self, state_dir: Path, *, wall_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000) -> None:
        self._codes = _JsonFile(state_dir / "pairing.json", {"codes": []})
        self._wall = wall_ms

    def _live(self) -> list[dict[str, Any]]:
        now = self._wall()
        codes: list[dict[str, Any]] = self._codes.read()["codes"]
        return [c for c in codes if c["expires_at"] > now]

    def create(self, role: Role, ttl_s: int = CODE_TTL_S) -> str:
        """A fresh code like ``ABCD-2345``. Only its hash is stored."""
        if role not in ROLES:
            raise ValueError("unknown role")
        live = self._live()
        if len(live) >= MAX_PENDING_CODES:
            raise OverflowError("too many unredeemed pairing codes")
        raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        entry = {"hash": _code_hash(raw), "role": role, "expires_at": self._wall() + ttl_s * 1000}
        self._codes.write({"codes": [*live, entry]})
        return f"{raw[:4]}-{raw[4:]}"

    def redeem(self, code: str) -> Role | None:
        """Consume a code. Returns its role, or ``None`` if unknown or expired. Single use."""
        if not 1 <= len(code) <= 32:
            return None
        wanted = _code_hash(code)
        live = self._live()
        match: dict[str, Any] | None = None
        for entry in live:  # constant-time per entry, no early exit
            if hmac.compare_digest(entry["hash"], wanted):
                match = entry
        if match is None:
            return None
        self._codes.write({"codes": [c for c in live if c is not match]})
        role: Role = match["role"]
        return role
