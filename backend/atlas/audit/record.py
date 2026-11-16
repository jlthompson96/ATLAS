"""audit-record.v1 in code: hashing, checkpoint signing input, and a dependency-free validator.

The Pi has no ``jsonschema`` (it is a dev-only dependency), so ``validate_record`` re-implements
contracts/audit-record.v1.schema.json by hand. ``tests/test_audit_record.py`` cross-checks it against the
real schema (golden chain, per-kind rules and mutation fuzzing), so the two cannot drift silently.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from typing import Any, Final

from atlas.audit.jcs import canonicalize

ZERO_HASH: Final = "0" * 64
CHECKPOINT_DOMAIN: Final = "atlas-audit-checkpoint-v1\n"

ACTORS: Final = frozenset(
    {
        "atlas-core",
        "atlas-voice",
        "atlas-egress",
        "atlas-audit",
        "atlas-ui",
        "atlas-research",
        "atlas-google",
        "atlas-discord",
        "atlas-executor",
        "atlas-panels",
        "atlas-market",
        "atlas-search",
        "atlas-fallback",
    }
)
KINDS: Final = (
    "genesis",
    "tool_call",
    "action_proposed",
    "approval",
    "write_ahead",
    "write_result",
    "egress",
    "power",
    "auth",
    "config",
    "system",
    "purge",
    "checkpoint",
)
CONTEXTS: Final = ("interactive", "unattended", "quarantined", "system")

# Kinds only atlas-audit itself may create (never accepted from a client).
INTERNAL_KINDS: Final = frozenset({"genesis", "purge", "checkpoint"})
# Kinds whose schema pins the actor (allOf in the schema).
KIND_ACTOR: Final = {
    "genesis": "atlas-audit",
    "approval": "atlas-core",
    "write_ahead": "atlas-executor",
    "write_result": "atlas-executor",
    "egress": "atlas-egress",
    "purge": "atlas-audit",
    "checkpoint": "atlas-audit",
}
# Kinds acknowledged only after fsync (PLAN 2.12: write actions are write-ahead logged; the rest of
# these are low-volume, security-relevant records that should survive a power cut).
DURABLE_KINDS: Final = frozenset(
    {"genesis", "action_proposed", "approval", "write_ahead", "write_result", "auth", "config", "purge", "checkpoint"}
)
DURABLE_SYSTEM_EVENTS: Final = frozenset({"chain_broken", "killswitch"})

# Fields a client may never supply: atlas-audit assigns them.
SERVER_ASSIGNED: Final = ("seq", "ts", "prev_hash", "hash")


def record_hash(record: dict[str, Any]) -> str:
    """``hex(SHA-256(JCS(record minus "hash")))``."""
    return hashlib.sha256(canonicalize({k: v for k, v in record.items() if k != "hash"})).hexdigest()


def checkpoint_message(to_seq: int, head_hash: str) -> bytes:
    """The exact bytes an Ed25519 checkpoint signature covers (audit-record.v1, Checkpoint.signature)."""
    return f"{CHECKPOINT_DOMAIN}{to_seq}\n{head_hash}".encode()


def is_durable(kind: str, record: dict[str, Any]) -> bool:
    if kind in DURABLE_KINDS:
        return True
    if kind == "system":
        system = record.get("system")
        return isinstance(system, dict) and system.get("event") in DURABLE_SYSTEM_EVENTS
    return False


# --- validator ---------------------------------------------------------------------------------------

Check = Callable[[Any, str], list[str]]


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _int(minimum: int | None = None, maximum: int | None = None) -> Check:
    def check(value: Any, path: str) -> list[str]:
        if not _is_int(value):
            return [f"{path}: must be an integer"]
        if minimum is not None and value < minimum:
            return [f"{path}: below minimum {minimum}"]
        if maximum is not None and value > maximum:
            return [f"{path}: above maximum {maximum}"]
        return []

    return check


def _bool(value: Any, path: str) -> list[str]:
    return [] if isinstance(value, bool) else [f"{path}: must be a boolean"]


def _enum(*allowed: str) -> Check:
    def check(value: Any, path: str) -> list[str]:
        return [] if isinstance(value, str) and value in allowed else [f"{path}: not one of {list(allowed)}"]

    return check


def _const(expected: Any) -> Check:
    def check(value: Any, path: str) -> list[str]:
        return [] if value == expected and type(value) is type(expected) else [f"{path}: must be {expected!r}"]

    return check


def _str(pattern: str | None = None, max_length: int | None = None) -> Check:
    rx = re.compile(pattern) if pattern else None

    def check(value: Any, path: str) -> list[str]:
        if not isinstance(value, str):
            return [f"{path}: must be a string"]
        if max_length is not None and len(value) > max_length:
            return [f"{path}: longer than {max_length}"]
        if rx is not None and rx.fullmatch(value) is None:
            return [f"{path}: does not match {pattern}"]
        return []

    return check


def _arr(item: Check, min_items: int = 0, max_items: int | None = None) -> Check:
    def check(value: Any, path: str) -> list[str]:
        if not isinstance(value, list):
            return [f"{path}: must be an array"]
        if len(value) < min_items:
            return [f"{path}: fewer than {min_items} items"]
        if max_items is not None and len(value) > max_items:
            return [f"{path}: more than {max_items} items"]
        errors: list[str] = []
        for i, element in enumerate(value):
            errors += item(element, f"{path}[{i}]")
        return errors

    return check


def _obj(required: tuple[str, ...], props: dict[str, Check]) -> Check:
    def check(value: Any, path: str) -> list[str]:
        if not isinstance(value, dict):
            return [f"{path}: must be an object"]
        errors = [f"{path}.{name}: required" for name in required if name not in value]
        for name, member in value.items():
            validator = props.get(name)
            if validator is None:
                errors.append(f"{path}.{name}: unknown property")
            else:
                errors += validator(member, f"{path}.{name}")
        return errors

    return check


def _no_float_json(value: Any, path: str) -> list[str]:
    if value is None or isinstance(value, bool | str) or _is_int(value):
        return []
    if isinstance(value, list):
        return [e for i, v in enumerate(value) for e in _no_float_json(v, f"{path}[{i}]")]
    if isinstance(value, dict):
        errors: list[str] = []
        for k, v in value.items():
            if not isinstance(k, str):
                errors.append(f"{path}: non-string key")
            else:
                errors += _no_float_json(v, f"{path}.{k}")
        return errors
    return [f"{path}: unsupported value (floats are not allowed)"]


def _args_redacted(value: Any, path: str) -> list[str]:
    if not isinstance(value, dict):
        return [f"{path}: must be an object"]
    if len(value) > 32:
        return [f"{path}: more than 32 properties"]
    return _no_float_json(value, path)


_ID = r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}"
_SHA = r"[0-9a-f]{64}"
_CODE = r"[A-Z][A-Z0-9_]{1,47}"

_DATA_REF = _obj(
    ("resource_id", "bytes", "sha256"),
    {
        "resource_id": _str(r"[a-z][a-z0-9_]*:[^\s]{1,300}"),
        "bytes": _int(0),
        "sha256": _str(_SHA),
        "tainted": _bool,
    },
)
_RESULT = _obj(
    ("status",),
    {"status": _enum("ok", "error", "denied", "refused", "timeout"), "code": _str(_CODE)},
)
_ACTION = _obj(
    ("action_id", "action_kind", "payload_sha256"),
    {
        "action_id": _str(_ID),
        "action_kind": _enum("email.send", "discord.post", "research.schedule", "portfolio.import"),
        "payload_sha256": _str(_SHA),
        "tainted": _bool,
        "approval_id": _str(_ID),
        "decision": _enum("approve", "deny"),
        "step_up": _bool,
        "expires_at": _int(0),
    },
)
_EGRESS = _obj(
    ("caller", "mode", "host", "decision"),
    {
        "caller": _str(r"atlas-[a-z]+"),
        "mode": _enum("connect", "fetch", "search"),
        "host": _str(max_length=253),
        "port": _int(1, 65535),
        "url": _str(max_length=2048),
        "query_sha256": _str(_SHA),
        "decision": _enum("allowed", "blocked"),
        "block_reason": _enum(
            "not_allowlisted",
            "private_address",
            "sni_mismatch",
            "budget_exceeded",
            "provenance",
            "denylisted",
            "kill_switch",
            "method",
            "malformed_request",
            "limit",
        ),
        "task_id": _str(_ID),
        "bytes_in": _int(0),
        "bytes_out": _int(0),
        "content_sha256": _str(_SHA),
    },
)
_POWER = _obj(
    ("event",),
    {
        "event": _enum(
            "wake_requested",
            "wake_refused",
            "wake_ready",
            "wake_failed",
            "sleep_requested",
            "sleep_accepted",
            "sleep_refused",
            "unexpected_wake",
            "lease_acquired",
            "lease_released",
        ),
        "wake_id": _str(_ID),
        "reason": _enum("interactive", "speculative", "scheduled_research", "manual", "retry", "idle", "queue_drained"),
        "stage": _enum("packet", "link", "api", "model"),
        "elapsed_ms": _int(0),
        "code": _str(_CODE),
    },
)
_AUTH = _obj(
    ("event",),
    {
        "event": _enum(
            "device_paired",
            "device_revoked",
            "ws_rejected",
            "stepup_verified",
            "stepup_failed",
            "portfolio_unlocked",
            "portfolio_unlock_failed",
            "oauth_renewed",
            "oauth_expired",
            "credential_rotated",
        ),
        "subject": _str(max_length=64),
    },
)
_CONFIG = _obj(
    ("key", "new_sha256"),
    {"key": _str(r"[a-z][a-z0-9_.]{0,63}"), "old_sha256": _str(_SHA), "new_sha256": _str(_SHA)},
)
_SYSTEM = _obj(
    ("event",),
    {
        "event": _enum(
            "service_start",
            "service_stop",
            "chain_verified",
            "chain_broken",
            "killswitch",
            "task_killed",
            "retention_run",
            "error_forwarded",
        ),
        "task_id": _str(_ID),
        "code": _str(_CODE),
    },
)
_PURGE = _obj(
    ("reason", "from_seq", "to_seq", "anchor_hash", "segments"),
    {
        "reason": _enum("retention", "user_request"),
        "from_seq": _int(0),
        "to_seq": _int(0),
        "anchor_hash": _str(_SHA),
        "segments": _arr(_str(_ID), min_items=1, max_items=400),
    },
)
_CHECKPOINT = _obj(
    ("from_seq", "to_seq", "head_hash", "sig_alg", "key_id", "signature"),
    {
        "from_seq": _int(0),
        "to_seq": _int(0),
        "head_hash": _str(_SHA),
        "sig_alg": _const("ed25519"),
        "key_id": _str(_ID),
        "signature": _str(r"[A-Za-z0-9_-]{86}"),
    },
)
_GENESIS = _obj(("chain_id",), {"chain_id": _str(_ID), "note": _str(max_length=200)})

_PROPS: Final[dict[str, Check]] = {
    "v": _const(1),
    "seq": _int(0),
    "ts": _int(0),
    "kind": _enum(*KINDS),
    "actor": _enum(*sorted(ACTORS)),
    "context": _enum(*CONTEXTS),
    "correlation_id": _str(_ID),
    "device_id": _str(_ID),
    "tool": _str(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*", 64),
    "args_redacted": _args_redacted,
    "data_accessed": _arr(_DATA_REF, max_items=256),
    "result": _RESULT,
    "action": _ACTION,
    "egress": _EGRESS,
    "power": _POWER,
    "auth": _AUTH,
    "config": _CONFIG,
    "system": _SYSTEM,
    "purge": _PURGE,
    "checkpoint": _CHECKPOINT,
    "genesis": _GENESIS,
    "prev_hash": _str(_SHA),
    "hash": _str(_SHA),
}
_REQUIRED: Final = ("v", "seq", "ts", "kind", "actor", "context", "prev_hash", "hash")

# dependentSchemas: property present -> kind restricted.
_ALLOWED_KINDS: Final[dict[str, tuple[str, ...]]] = {
    "action": ("action_proposed", "approval", "write_ahead", "write_result"),
    "egress": ("egress",),
    "power": ("power",),
    "auth": ("auth",),
    "config": ("config",),
    "system": ("system",),
    "purge": ("purge",),
    "checkpoint": ("checkpoint",),
    "genesis": ("genesis",),
    "tool": ("tool_call", "action_proposed", "write_ahead", "write_result"),
}
# allOf/if-then: kind -> required top-level properties.
_KIND_REQUIRED: Final[dict[str, tuple[str, ...]]] = {
    "genesis": ("genesis",),
    "tool_call": ("tool", "args_redacted", "data_accessed", "result"),
    "action_proposed": ("tool", "action"),
    "approval": ("action", "device_id"),
    "write_ahead": ("tool", "action"),
    "write_result": ("tool", "action", "result"),
    "egress": ("egress", "result"),
    "power": ("power",),
    "auth": ("auth", "result"),
    "config": ("config",),
    "system": ("system",),
    "purge": ("purge",),
    "checkpoint": ("checkpoint",),
}
_KIND_ACTION_REQUIRED: Final[dict[str, tuple[str, ...]]] = {
    "action_proposed": ("tainted",),
    "approval": ("decision", "step_up", "approval_id"),
    "write_ahead": ("approval_id",),
    "write_result": ("approval_id",),
}


def validate_record(record: Any) -> list[str]:
    """Return every way ``record`` violates audit-record.v1 (empty list means valid)."""
    if not isinstance(record, dict):
        return ["record: must be an object"]
    errors = _obj(_REQUIRED, _PROPS)(record, "record")
    kind = record.get("kind")
    if not isinstance(kind, str) or kind not in KINDS:
        return errors
    for prop, kinds in _ALLOWED_KINDS.items():
        if prop in record and kind not in kinds:
            errors.append(f"record.{prop}: not allowed for kind {kind}")
    errors += [
        f"record.{name}: required for kind {kind}" for name in _KIND_REQUIRED.get(kind, ()) if name not in record
    ]
    pinned_actor = KIND_ACTOR.get(kind)
    if pinned_actor is not None and record.get("actor") != pinned_actor:
        errors.append(f"record.actor: kind {kind} requires {pinned_actor}")
    action = record.get("action")
    if isinstance(action, dict):
        errors += [
            f"record.action.{name}: required for kind {kind}"
            for name in _KIND_ACTION_REQUIRED.get(kind, ())
            if name not in action
        ]
    if kind == "genesis":
        if record.get("seq") != 0:
            errors.append("record.seq: genesis must be 0")
        if record.get("prev_hash") != ZERO_HASH:
            errors.append("record.prev_hash: genesis must be 64 zeros")
    if kind == "checkpoint" and record.get("context") != "system":
        errors.append("record.context: checkpoint must be 'system'")
    return errors


def validate_submission(body: Any, peer_actor: str) -> list[str]:
    """Check a record body a *client* submitted, before atlas-audit assigns seq/ts/actor/hashes.

    ``peer_actor`` comes from the socket peer credentials. A body that names an actor must name the
    same one (a mismatch is a spoofing attempt and is refused), and a client can never supply the
    fields atlas-audit assigns or create the kinds only atlas-audit may write.
    """
    if not isinstance(body, dict):
        return ["body: must be an object"]
    errors: list[str] = []
    claimed = body.get("actor")
    if claimed is not None and claimed != peer_actor:
        errors.append("ACTOR_MISMATCH")
    errors += [f"{name}: assigned by atlas-audit" for name in SERVER_ASSIGNED if name in body]
    if body.get("v", 1) != 1 or isinstance(body.get("v"), bool):
        errors.append("v: must be 1")
    kind = body.get("kind")
    if kind in INTERNAL_KINDS:
        errors.append(f"kind: {kind} is written by atlas-audit only")
    for name in INTERNAL_KINDS:  # the matching sub-objects are internal too
        if name in body:
            errors.append(f"{name}: written by atlas-audit only")
    pinned = KIND_ACTOR.get(kind) if isinstance(kind, str) else None
    if pinned is not None and peer_actor != pinned:
        errors.append(f"actor: kind {kind} may only be submitted by {pinned}")
    return errors
