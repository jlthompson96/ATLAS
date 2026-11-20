"""Session-level behaviour that needs no sockets: inactivity privacy, frame validation, PIN verifier, dev TLS."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from atlas.ui_server.devices import Device
from atlas.ui_server.devtls import make_dev_tls
from atlas.ui_server.model import Model
from atlas.ui_server.pin import PinVerifier, hash_pin
from atlas.ui_server.security import FailureLimiter
from atlas.ui_server.session import Session
from tests.ui_support import FAST_HASHER

Json = dict[str, Any]
T0 = 1_763_279_000_000


class FakeBackend:
    def __init__(self) -> None:
        self.model = Model(state={"state": "idle"})
        self.version = 0
        self.server_version = "test"
        self.boot_id = "boot-test"
        self.calls: list[tuple[str, Json]] = []
        self.online = True

    async def core_call(self, op: str, **fields: Any) -> Json | None:
        self.calls.append((op, fields))
        return {"ok": True} if self.online else None

    def save_layouts(self) -> None:
        pass


class Rig:
    def __init__(self, role: str = "remote", pin_hash: str | None = None) -> None:
        self.frames: list[Json] = []
        self.now = [T0]
        self.mono = [0.0]
        self.backend = FakeBackend()
        self.pin = PinVerifier(pin_hash or hash_pin("1234", FAST_HASHER), hasher=FAST_HASHER)
        self.session = Session(
            self._collect,
            device=Device("dev-1", role, T0),  # type: ignore[arg-type]
            backend=self.backend,
            pin=self.pin,
            wall_ms=lambda: self.now[0],
            monotonic=lambda: self.mono[0],
        )

    async def _collect(self, raw: str) -> None:
        self.frames.append(json.loads(raw))

    async def client(self, seq: int, type_: str, payload: Json | None = None) -> None:
        await self.session.handle_raw(
            json.dumps({"v": 1, "type": type_, "seq": seq, "ts": 0, "payload": payload or {}})
        )

    def types(self) -> list[str]:
        return [f["type"] for f in self.frames]


def test_inactivity_turns_privacy_on_per_role_window() -> None:
    async def go() -> None:
        for role, window in (("remote", 300), ("kiosk", 600)):
            rig = Rig(role)
            await rig.session.snapshot()
            rig.mono[0] = window - 1
            assert not await rig.session.check_inactivity()
            rig.mono[0] = window
            assert await rig.session.check_inactivity()
            types = rig.types()
            assert types[-2:] == ["privacy.mode", "sync.snapshot"]  # contract: a snapshot follows every change
            assert rig.frames[-2]["payload"] == {"on": True, "reason": "inactivity"}
            assert rig.frames[-1]["payload"]["privacy"]["on"] is True
            assert not await rig.session.check_inactivity()  # already on

    asyncio.run(go())


def test_real_interaction_resets_the_inactivity_timer_but_pings_do_not() -> None:
    async def go() -> None:
        rig = Rig("remote")
        await rig.session.snapshot()
        rig.mono[0] = 200
        await rig.client(1, "clock.ping", {"client_t": 1.0})
        await rig.client(2, "sync.request")
        rig.mono[0] = 301
        assert await rig.session.check_inactivity()  # clock.ping and sync.request don't count
        rig2 = Rig("remote")
        await rig2.session.snapshot()
        rig2.mono[0] = 200
        await rig2.client(1, "activity.ping")
        rig2.mono[0] = 301
        assert not await rig2.session.check_inactivity()  # 101 s since real input

    asyncio.run(go())


def test_a_frame_that_breaks_the_state_machine_is_dropped_and_followed_by_a_snapshot() -> None:
    async def go() -> None:
        rig = Rig()
        await rig.session.snapshot()
        backend = rig.backend
        backend.version = 1
        await rig.session.push("state.changed", {"state": "speaking"}, 1)  # idle -> speaking is illegal
        assert rig.session.dropped == 1
        assert rig.types() == ["sync.snapshot", "sync.snapshot"]
        assert [f["seq"] for f in rig.frames] == [0, 1]  # the dropped frame consumed no sequence number
        backend.model.state = {"state": "thinking"}
        backend.version = 2
        await rig.session.push("state.changed", {"state": "thinking"}, 2)
        assert rig.types()[-1] == "state.changed" and rig.frames[-1]["seq"] == 2

    asyncio.run(go())


def test_schema_invalid_events_from_core_are_dropped_without_a_snapshot() -> None:
    async def go() -> None:
        rig = Rig()
        await rig.session.snapshot()
        rig.backend.version = 1
        await rig.session.push("alert", {"alert_id": "a", "level": "loud", "code": "X", "message": "m"}, 1)
        await rig.session.push("mystery.event", {}, 1)
        assert rig.session.dropped == 2 and rig.types() == ["sync.snapshot"]

    asyncio.run(go())


def test_events_already_in_a_snapshot_are_not_replayed() -> None:
    async def go() -> None:
        rig = Rig()
        alert = {"alert_id": "a1", "level": "info", "code": "CORE_RECOVERED", "message": "m"}
        rig.backend.model.apply("alert", alert)
        rig.backend.version = 5
        await rig.session.snapshot()  # includes a1 (version 5)
        await rig.session.push("alert", alert, 5)  # queued before the snapshot: skipped
        assert rig.types() == ["sync.snapshot"]
        await rig.session.push("alert", {**alert, "alert_id": "a2"}, 6)
        assert rig.types() == ["sync.snapshot", "alert"]

    asyncio.run(go())


def test_portfolio_and_voice_report_honestly_that_they_are_not_available() -> None:
    async def go() -> None:
        rig = Rig("remote")
        await rig.session.snapshot()
        await rig.client(1, "portfolio.unlock", {"passphrase": "x"})
        await rig.client(2, "ptt.start")
        results = [f["payload"] for f in rig.frames if f["type"] == "request.result"]
        assert [(r["ok"], r["code"]) for r in results] == [(False, "NOT_AVAILABLE"), (False, "VOICE_UNAVAILABLE")]

    asyncio.run(go())


def test_step_up_fails_closed_when_core_is_down() -> None:
    async def go() -> None:
        rig = Rig("remote")
        await rig.session.snapshot()
        rig.backend.online = False
        await rig.client(1, "stepup.verify", {"pin": "1234"})
        assert "stepup.granted" not in rig.types()
        assert rig.frames[-1]["payload"]["code"] == "CORE_UNREACHABLE"

    asyncio.run(go())


def test_step_up_token_registered_with_core_matches_the_one_granted() -> None:
    async def go() -> None:
        rig = Rig("remote")
        await rig.session.snapshot()
        await rig.client(1, "stepup.verify", {"pin": "1234"})
        granted = next(f for f in rig.frames if f["type"] == "stepup.granted")["payload"]
        (op, fields) = rig.backend.calls[0]
        assert op == "stepup.register" and fields["device_id"] == "dev-1"
        assert fields["token"] == granted["token"] and fields["expires_at"] == granted["expires_at"] == T0 + 60_000

    asyncio.run(go())


def test_pin_verifier_ok_bad_locked_and_reset() -> None:
    async def go() -> None:
        now = [0.0]
        dev = FailureLimiter(3, 600, 300, clock=lambda: now[0])
        glob = FailureLimiter(100, 600, 300, clock=lambda: now[0])
        pin = PinVerifier(hash_pin("1234", FAST_HASHER), hasher=FAST_HASHER, per_device=dev, global_limit=glob)
        assert await pin.verify("d", "0000") == "bad"
        assert await pin.verify("d", "1234") == "ok"  # success resets the count
        assert [await pin.verify("d", "0000") for _ in range(3)] == ["bad", "bad", "locked"]
        assert await pin.verify("d", "1234") == "locked"
        assert pin.locked("d") and not pin.locked("other")
        now[0] += 301
        assert await pin.verify("d", "1234") == "ok"

    asyncio.run(go())


def test_pin_hash_format_and_validation() -> None:
    assert hash_pin("0000").startswith("$argon2id$")
    for bad in ("123", "1234567890123", "12a4", ""):
        with pytest.raises(ValueError, match="4 to 12 digits"):
            hash_pin(bad)
    garbage = PinVerifier("not-a-hash", hasher=FAST_HASHER)
    assert asyncio.run(garbage.verify("d", "1234")) == "bad"  # a corrupt credential never verifies


def test_dev_tls_makes_a_usable_chain_and_reuses_it(tmp_path: Path) -> None:
    ca, cert, key = make_dev_tls(tmp_path / "tls", ["atlas.lan"])
    assert key.stat().st_mode & 0o777 == 0o600
    before = cert.read_bytes()
    assert make_dev_tls(tmp_path / "tls")[1].read_bytes() == before
    from cryptography import x509

    leaf = x509.load_pem_x509_certificate(cert.read_bytes())
    names = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "atlas.lan" in names.get_values_for_type(x509.DNSName) and "localhost" in names.get_values_for_type(
        x509.DNSName
    )
    assert ca.read_text().startswith("-----BEGIN CERTIFICATE-----")
