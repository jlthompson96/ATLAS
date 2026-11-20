"""UW-3 (PLAN.md §8.8) and AP-1/AP-3: atlas-executor refuses anything without a valid human approval.

Every refusal case also checks that nothing was sent and no write_ahead was written.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
from collections.abc import Callable, Coroutine
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from atlas.audit.peercred import PeerCredentialError, SockLike
from atlas.core.auditing import RecordingAuditSink
from atlas.core.executor_link import SocketExecutorLink
from atlas.executor.approval_record import (
    ApprovalKeyError,
    ApprovalRecord,
    ApprovalSigner,
    SignedApproval,
    load_signing_key,
    load_verify_key,
    new_approval_id,
    new_nonce,
    public_key_text,
)
from atlas.executor.senders import FakeSender
from atlas.executor.server import ExecutorServer
from atlas.executor.service import Executor
from atlas.executor.verify import ApprovalVerifier, NonceStore
from atlas.policy.payloads import canonical_text, payload_sha256
from tests.core_support import FakeClock

PAYLOAD = {"kind": "email.send", "to": ["dana@example.org"], "cc": [], "subject": "Re: Saturday", "body": "2pm works."}
CANONICAL = canonical_text(PAYLOAD)


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


class Rig:
    def __init__(self, tmp_path: Path | None = None) -> None:
        self.clock = FakeClock()
        self.key = Ed25519PrivateKey.generate()
        self.signer = ApprovalSigner(self.key)
        self.nonces = NonceStore(":memory:" if tmp_path is None else tmp_path / "nonces.sqlite3")
        self.verifier = ApprovalVerifier(self.key.public_key(), self.nonces, self.clock.wall)
        self.audit = RecordingAuditSink("atlas-executor")
        self.sender = FakeSender("email.send")
        self.executor = Executor(self.verifier, self.audit, {"email.send": self.sender})

    def record(self, **change: Any) -> ApprovalRecord:
        now = self.clock.wall()
        base = ApprovalRecord(
            approval_id=new_approval_id(),
            action_id="act-1",
            action_kind="email.send",
            payload_sha256=payload_sha256(CANONICAL),
            origin_context="interactive",
            decision_context="interactive",
            approved_via="hud",
            device_id="hud-remote",
            decision="approve",
            step_up=True,
            action_created_at=now - 10_000,
            decided_at=now - 1_000,
            expires_at=now + 60_000,
            nonce=new_nonce(),
            key_id=self.signer.key_id,
        )
        return replace(base, **change)

    def envelope(self, record: ApprovalRecord | None = None, payload: str = CANONICAL) -> dict[str, Any]:
        return self.signer.sign(record or self.record()).envelope(payload)

    def execute(self, envelope: Any) -> Any:
        return run(self.executor.execute(envelope)).to_json()

    def assert_nothing_happened(self) -> None:
        assert self.sender.sent == []
        assert self.executor.executions == 0
        assert self.audit.of_kind("write_ahead") == []


def test_a_valid_approval_executes_once_with_write_ahead_then_result() -> None:
    rig = Rig()
    assert rig.execute(rig.envelope()) == {"ok": True, "status": "executed"}
    assert rig.sender.sent == [PAYLOAD]
    kinds = [r["kind"] for r in rig.audit.records]
    assert kinds == ["write_ahead", "write_result"]
    ahead, result = rig.audit.records
    assert ahead["action"]["approval_id"] == result["action"]["approval_id"]
    assert result["result"] == {"status": "ok"}
    assert "dana@example.org" not in json.dumps(rig.audit.records)  # hashes and IDs only


# --- UW-3: forged record ----------------------------------------------------------------------------------


def test_uw3_record_signed_by_another_key_is_refused() -> None:
    rig = Rig()
    forger = ApprovalSigner(Ed25519PrivateKey.generate())
    record = rig.record(key_id=forger.key_id)
    assert rig.execute(forger.sign(record).envelope(CANONICAL)) == {"ok": False, "code": "UNKNOWN_KEY"}
    # Even claiming the real key id, a foreign signature does not verify.
    forged = SignedApproval(rig.record(), forger.sign(replace(rig.record(), key_id=forger.key_id)).signature)
    assert rig.execute(forged.envelope(CANONICAL)) == {"ok": False, "code": "BAD_SIGNATURE"}
    rig.assert_nothing_happened()


@pytest.mark.parametrize(
    "field_change",
    [
        {"action_id": "act-2"},
        {"payload_sha256": "0" * 64},
        {"expires_at": 9_999_999_999_999},
        {"origin_context": "unattended"},
        {"device_id": "kiosk-1"},
        {"decided_at": 1},
    ],
)
def test_uw3_any_edit_after_signing_breaks_the_signature(field_change: dict[str, Any]) -> None:
    rig = Rig()
    env = rig.envelope()
    env["approval"].update(field_change)
    assert rig.execute(env) == {"ok": False, "code": "BAD_SIGNATURE"}
    rig.assert_nothing_happened()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e: e.update(signature="A" * 86),
        lambda e: e.update(signature="short"),
        lambda e: e.update(signature=None),
        lambda e: e.pop("signature"),
        lambda e: e.update(extra=1),
        lambda e: e.update(op="run"),
        lambda e: e["approval"].update(extra=1),
        lambda e: e["approval"].pop("nonce"),
        lambda e: e["approval"].update(v=2),
        lambda e: e["approval"].update(step_up="true"),
        lambda e: e.update(approval="not an object"),
    ],
)
def test_uw3_malformed_envelopes_are_refused(mutate: Callable[[dict[str, Any]], Any]) -> None:
    rig = Rig()
    env = rig.envelope()
    mutate(env)
    result = rig.execute(env)
    assert result["ok"] is False and result["code"] in ("BAD_REQUEST", "BAD_RECORD", "BAD_SIGNATURE")
    rig.assert_nothing_happened()


# --- UW-3: wrong hash (AP-1) ---------------------------------------------------------------------------------


def test_uw3_payload_changed_after_approval_is_refused() -> None:
    rig = Rig()
    changed = canonical_text({**PAYLOAD, "to": ["attacker@example.com"]})
    assert rig.execute(rig.envelope(payload=changed)) == {"ok": False, "code": "HASH_MISMATCH"}
    rig.assert_nothing_happened()


def test_non_canonical_payload_with_the_right_hash_is_refused() -> None:
    rig = Rig()
    spaced = json.dumps(PAYLOAD)  # same object, different bytes
    record = rig.record(payload_sha256=payload_sha256(spaced))
    assert rig.execute(rig.envelope(record, spaced)) == {"ok": False, "code": "NON_CANONICAL_PAYLOAD"}
    rig.assert_nothing_happened()


def test_payload_kind_must_match_the_signed_kind_and_be_supported() -> None:
    rig = Rig()
    post = canonical_text(
        {"kind": "discord.post", "channel_id": "110000000000000055", "channel_label": "#b", "content": "x"}
    )
    record = rig.record(payload_sha256=payload_sha256(post))
    assert rig.execute(rig.envelope(record, post)) == {"ok": False, "code": "KIND_MISMATCH"}
    sched = canonical_text({"kind": "research.schedule", "question": "q", "every_minutes": 60})
    record = rig.record(action_kind="research.schedule", payload_sha256=payload_sha256(sched))
    assert rig.execute(rig.envelope(record, sched)) == {"ok": False, "code": "UNSUPPORTED_KIND"}
    bad = canonical_text({**PAYLOAD, "subject": "a\nBcc: x@example.com"})
    record = rig.record(payload_sha256=payload_sha256(bad))
    assert rig.execute(rig.envelope(record, bad)) == {"ok": False, "code": "BAD_PAYLOAD"}
    rig.assert_nothing_happened()


# --- UW-3: expired (AP-3) -------------------------------------------------------------------------------------


def test_uw3_expired_approval_is_refused() -> None:
    rig = Rig()
    env = rig.envelope()
    rig.clock.advance(61)
    assert rig.execute(env) == {"ok": False, "code": "EXPIRED"}
    rig.assert_nothing_happened()


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"expires_at_delta": 11 * 60_000}, "EXPIRED"),  # longer than the 10-minute maximum lifetime
        ({"expires_at_delta": 0}, "EXPIRED"),
        ({"decided_in": 60_000}, "NOT_YET_VALID"),
    ],
)
def test_approval_lifetime_rules(change: dict[str, int], code: str) -> None:
    rig = Rig()
    now = rig.clock.wall()
    decided = now + change.get("decided_in", -1000)
    record = rig.record(decided_at=decided, expires_at=decided + change.get("expires_at_delta", 60_000))
    assert rig.execute(rig.envelope(record)) == {"ok": False, "code": code}
    rig.assert_nothing_happened()


# --- UW-3: replayed -------------------------------------------------------------------------------------------


def test_uw3_replayed_approval_is_refused(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    env = rig.envelope()
    assert rig.execute(env)["ok"] is True
    assert rig.execute(env) == {"ok": False, "code": "REPLAYED"}
    # A different approval for the same action is a replay too (one execution per action).
    assert rig.execute(rig.envelope(rig.record(approval_id=new_approval_id()))) == {"ok": False, "code": "REPLAYED"}
    # And the claim survives a restart of the executor.
    restarted = ApprovalVerifier(rig.key.public_key(), NonceStore(tmp_path / "nonces.sqlite3"), rig.clock.wall)
    again = Executor(restarted, rig.audit, {"email.send": rig.sender})
    assert run(again.execute(env)).to_json() == {"ok": False, "code": "REPLAYED"}
    assert len(rig.sender.sent) == 1


# --- UW-3: unattended without a later human approval -------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"decided_at_equals_created": True}, "CONTEXT_REJECTED"),
        ({"approved_via": "unattended"}, "NOT_APPROVED"),
        ({"approved_via": "voice"}, "NOT_APPROVED"),
        ({"decision_context": "unattended"}, "NOT_APPROVED"),
        ({"step_up": False}, "NOT_APPROVED"),
        ({"decision": "deny"}, "NOT_APPROVED"),
    ],
)
def test_uw3_unattended_action_needs_a_later_human_approval(change: dict[str, Any], code: str) -> None:
    rig = Rig()
    fields = {k: v for k, v in change.items() if k != "decided_at_equals_created"}
    record = rig.record(origin_context="unattended", **fields)
    if change.get("decided_at_equals_created"):
        record = replace(record, decided_at=record.action_created_at)
    assert rig.execute(rig.envelope(record)) == {"ok": False, "code": code}
    rig.assert_nothing_happened()


def test_unattended_action_with_a_later_hud_approval_executes() -> None:
    rig = Rig()
    assert rig.execute(rig.envelope(rig.record(origin_context="unattended")))["ok"] is True


# --- write-ahead must be durable -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sink",
    [
        RecordingAuditSink("atlas-executor", fail_with="AUDIT_UNAVAILABLE"),
        RecordingAuditSink("atlas-executor", durable=False),
    ],
)
def test_nothing_is_sent_unless_the_write_ahead_is_durable(sink: RecordingAuditSink) -> None:
    rig = Rig()
    executor = Executor(rig.verifier, sink, {"email.send": rig.sender})
    env = rig.envelope()
    assert run(executor.execute(env)).to_json() == {"ok": False, "code": "AUDIT_UNAVAILABLE"}
    assert rig.sender.sent == [] and executor.executions == 0
    # The approval is spent: an audit outage never turns into a later automatic retry. The user re-approves.
    assert run(executor.execute(env)).to_json() == {"ok": False, "code": "REPLAYED"}
    assert rig.sender.sent == []


def test_send_failure_is_recorded_in_write_result() -> None:
    rig = Rig()
    failing = FakeSender("email.send", fail_with="GRANT_EXPIRED")
    executor = Executor(rig.verifier, rig.audit, {"email.send": failing})
    assert run(executor.execute(rig.envelope())).to_json() == {"ok": False, "code": "GRANT_EXPIRED"}
    assert rig.audit.records[-1]["result"] == {"status": "error", "code": "GRANT_EXPIRED"}


def test_sender_must_be_registered_under_its_own_kind() -> None:
    rig = Rig()
    with pytest.raises(ValueError):
        Executor(rig.verifier, rig.audit, {"discord.post": FakeSender("email.send")})


# --- keys ------------------------------------------------------------------------------------------------------


def test_signing_key_round_trip_and_bad_keys(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    path = tmp_path / "approval-signing-key"
    path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    loaded = load_signing_key(path)
    assert ApprovalSigner(loaded).key_id == ApprovalSigner(key).key_id
    assert public_key_text(load_verify_key(public_key_text(key))) == public_key_text(key)
    path.write_bytes(b"not a key")
    with pytest.raises(ApprovalKeyError):
        load_signing_key(path)
    with pytest.raises(ApprovalKeyError):
        load_verify_key("@@@")


def test_signer_refuses_records_for_another_key() -> None:
    rig = Rig()
    with pytest.raises(ValueError):
        rig.signer.sign(rig.record(key_id="ak-0000000000000000"))


# --- the socket -------------------------------------------------------------------------------------------------


class Creds:
    def __init__(self, uid: int | None) -> None:
        self.uid = uid

    def peer_uid(self, sock: SockLike) -> int:
        if self.uid is None:
            raise PeerCredentialError("none")
        return self.uid


class Users:
    def user_name(self, uid: int) -> str | None:
        return {1: "atlas-core", 2: "atlas-ui", 3: "atlas-google"}.get(uid)


@pytest.mark.parametrize(("uid", "ok"), [(1, True), (2, False), (3, False), (None, False), (99, False)])
def test_socket_accepts_atlas_core_only(tmp_path: Path, uid: int | None, ok: bool) -> None:
    rig = Rig()
    path = Path(os.environ.get("TMPDIR", "/tmp")) / f"ex-{os.getpid()}-{uid}.sock"  # noqa: S108 - short path for AF_UNIX

    async def go() -> dict[str, Any]:
        server = ExecutorServer(rig.executor, Creds(uid), Users())
        await server.listen_path(path)
        try:
            return await SocketExecutorLink(path).submit(rig.envelope())
        finally:
            await server.close()
            path.unlink(missing_ok=True)

    reply = run(go())
    if ok:
        assert reply == {"ok": True, "status": "executed"}
    else:
        assert reply == {"ok": False, "code": "PEER_REJECTED"}
        rig.assert_nothing_happened()


def test_socket_refuses_garbage_and_link_reports_unavailable(tmp_path: Path) -> None:
    rig = Rig()
    server = ExecutorServer(rig.executor, Creds(1), Users())
    for line in (b"not json\n", b"[]\n", b'{"op":"execute"}\n', b"\xff\xfe\n"):
        reply = json.loads(run(server.dispatch(line)))
        assert reply["ok"] is False and reply["code"] in ("BAD_REQUEST",)
    rig.assert_nothing_happened()
    missing = SocketExecutorLink(tmp_path / "nope.sock", timeout_s=0.5)
    assert run(missing.submit({})) == {"ok": False, "code": "EXECUTOR_UNAVAILABLE"}
    assert socket.AF_UNIX
