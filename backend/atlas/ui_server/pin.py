"""Step-up PIN verification (PLAN.md section 4.5; contracts/services.md section 4: ``stepup-pin-hash``).

The hash is an Argon2id PHC string (``$argon2id$v=19$m=...``) read from the systemd credential. Verification
runs in a worker thread so it can't stall the event loop, after a lockout check, and failures are counted per
device and globally (a LAN attacker with one paired device can't get unlimited guesses by reconnecting, and
a spray over many devices hits the global limit).

Under the current contract the PIN is verified here, in atlas-ui; a proposal
(contracts/proposals/2026-11-18-security-core-followups.md, F3) would move it into core. Not implemented.
"""

from __future__ import annotations

import asyncio
import getpass
import sys
from typing import Final, Literal

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from atlas.ui_server.security import FailureLimiter

PinResult = Literal["ok", "bad", "locked"]
GLOBAL_KEY: Final = "*"
PIN_RE_MIN: Final = 4
PIN_RE_MAX: Final = 12


def hash_pin(pin: str, hasher: PasswordHasher | None = None) -> str:
    if not (PIN_RE_MIN <= len(pin) <= PIN_RE_MAX and pin.isdigit()):
        raise ValueError("the PIN must be 4 to 12 digits")
    return (hasher or PasswordHasher()).hash(pin)


class PinVerifier:
    def __init__(
        self,
        pin_hash: str,
        *,
        hasher: PasswordHasher | None = None,
        per_device: FailureLimiter | None = None,
        global_limit: FailureLimiter | None = None,
    ) -> None:
        self._hash = pin_hash
        self._hasher = hasher or PasswordHasher()
        # 5 wrong PINs in 10 minutes locks that device for 5 minutes; 15 across all devices locks everyone.
        self._device = per_device or FailureLimiter(5, 600, 300)
        self._global = global_limit or FailureLimiter(15, 600, 300)

    def locked(self, device_id: str) -> bool:
        return self._device.locked(device_id) or self._global.locked(GLOBAL_KEY)

    async def verify(self, device_id: str, pin: str) -> PinResult:
        if self.locked(device_id):
            return "locked"
        try:
            await asyncio.to_thread(self._hasher.verify, self._hash, pin)
        except (VerificationError, InvalidHashError):
            self._device.failure(device_id)
            self._global.failure(GLOBAL_KEY)
            return "locked" if self.locked(device_id) else "bad"
        self._device.success(device_id)
        return "ok"


def main() -> int:
    """``python -m atlas.ui_server.pin``: hash a PIN read from the terminal (never from argv)."""
    pin = getpass.getpass("New step-up PIN (4-12 digits): ")
    if pin != getpass.getpass("Again: "):
        print("PINs differ", file=sys.stderr)
        return 1
    print(hash_pin(pin))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
