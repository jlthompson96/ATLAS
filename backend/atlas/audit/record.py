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
# Who may write what (allOf in the schema; rev 1.2.0, proposal 2026-11-22-audit-actor-pinning). A kind listed in
# KIND_ACTORS is pinned whatever its event. For auth and system, EVENT_ACTORS pins single events; an event in neither
# table (system service_start, service_stop, error_forwarded) may come from any service, because it describes the
# writer itself.
_CORE: Final = frozenset({"atlas-core"})
_AUDIT: Final = frozenset({"atlas-audit"})
_EXECUTOR: Final = frozenset({"atlas-executor"})
KIND_ACTORS: Final[dict[str, frozenset[str]]] = {
    "genesis": _AUDIT,
    "tool_call": _CORE,
    "action_proposed": _CORE,
    "approval": _CORE,
    "write_ahead": _EXECUTOR,
    "write_result": _EXECUTOR,
    "egress": frozenset({"atlas-egress"}),
    "power": _CORE,
    "config": frozenset({"atlas-core", "atlas-ui"}),
    "purge": _AUDIT,
    "checkpoint": _AUDIT,
}
_UI: Final = frozenset({"atlas-ui"})
EVENT_ACTORS: Final[dict[str, dict[str, frozenset[str]]]] = {
    "auth": {
        "device_paired": _UI,
        "device_revoked": _UI,
        "ws_rejected": _UI,
        "stepup_verified": _UI,
        "stepup_failed": _UI,
        "portfolio_unlocked": frozenset({"atlas-market"}),
        "portfolio_unlock_failed": frozenset({"atlas-market"}),
        "oauth_renewed": frozenset({"atlas-google", "atlas-executor"}),
        "oauth_expired": frozenset({"atlas-google", "atlas-executor"}),
    },
    "system": {
        "chain_verified": _AUDIT,
        "chain_broken": _AUDIT,
        "retention_run": _AUDIT,
        "killswitch": _CORE,
        "task_killed": _CORE,
    },
}
# auth credential_rotated: any service, for one of its own credentials only (contracts/services.md section 4;
# test_audit_record.py checks this table against that file and against the schema).
SERVICE_CREDENTIALS: Final[dict[str, frozenset[str]]] = {
    "atlas-core": frozenset({"lmstudio-api-token", "approval-signing-key"}),
    "atlas-ui": frozenset({"tls-key", "device-token-key", "stepup-pin-hash"}),
    "atlas-audit": frozenset({"audit-checkpoint-key", "audit-segment-master-key"}),
    "atlas-google": frozenset({"google-oauth-client", "google-read-refresh-token"}),
    "atlas-discord": frozenset({"discord-reader-bot-token"}),
    "atlas-executor": frozenset({"google-oauth-client", "google-send-refresh-token", "discord-poster-bot-token"}),
    "atlas-market": frozenset({"twelvedata-api-key"}),
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


def _args_redacted(value: Any, path: str) -> list[str]:
    if not isinstance(value, dict):
        return [f"{path}: must be an object"]
    if len(value) > 32:
        return [f"{path}: more than 32 properties"]
    return _no_float_json(value, path)


MAX_ARGS_DEPTH: Final = 8  # nesting levels inside args_redacted (the object itself is level 1)
MAX_ARGS_NODES: Final = 1000  # total values inside args_redacted


def _no_float_json(root: Any, path: str) -> list[str]:
    """Iterative walk (never recursive: a 1,100-deep body must not raise RecursionError) with depth and size caps."""
    stack: list[tuple[Any, int]] = [(root, 1)]
    nodes = 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > MAX_ARGS_NODES:
            return [f"{path}: more than {MAX_ARGS_NODES} values"]
        if depth > MAX_ARGS_DEPTH:
            return [f"{path}: nested deeper than {MAX_ARGS_DEPTH}"]
        if value is None or isinstance(value, bool | str) or _is_int(value):
            continue
        if isinstance(value, list):
            stack.extend((item, depth + 1) for item in value)
        elif isinstance(value, dict):
            if not all(isinstance(k, str) for k in value):
                return [f"{path}: non-string key"]
            stack.extend((item, depth + 1) for item in value.values())
        else:
            return [f"{path}: unsupported value (floats are not allowed)"]
    return []


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
    errors += [f"record.{e}" for e in actor_errors(kind, record, record.get("actor"))]
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
    if isinstance(kind, str):
        errors += actor_errors(kind, body, peer_actor)
    return errors


def _sub_event(record: dict[str, Any], kind: str) -> Any:
    sub = record.get(kind)
    return sub.get("event") if isinstance(sub, dict) else None


def actor_errors(kind: str, record: dict[str, Any], actor: Any) -> list[str]:
    """The ways ``actor`` may not write this ``kind`` (and auth/system event) of ``record``: the schema's actor pins.

    A missing or malformed sub-object is left to the shape checks; only a recognised event is pinned, exactly as the
    schema's ``if`` branches only match a recognised event.
    """
    # Anything read from a record may be any JSON value; only strings are looked up, so a list never raises here.
    name = actor if isinstance(actor, str) else None
    event = _sub_event(record, kind)
    event = event if isinstance(event, str) else None
    allowed = KIND_ACTORS.get(kind)
    if allowed is None and kind in EVENT_ACTORS and event is not None:
        allowed = EVENT_ACTORS[kind].get(event)
    if allowed is not None:
        if name not in allowed:
            return [f"actor: kind {kind} may only be written by {', '.join(sorted(allowed))}"]
        return []
    if kind == "auth" and event == "credential_rotated":
        subject = record["auth"].get("subject")
        if subject is None:
            return ["auth.subject: required for credential_rotated"]
        own = SERVICE_CREDENTIALS.get(name, frozenset()) if name is not None else frozenset()
        if not isinstance(subject, str) or subject not in own:
            return ["actor: credential_rotated may only name one of the writer's own credentials"]
    return []
