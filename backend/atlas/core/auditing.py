"""Audit records written by atlas-core and atlas-executor (audit-record.v1, PLAN.md §2.12).

``AuditSink`` is the structural type of ``atlas.audit.client.AuditClient.append``; production passes
the real client, tests pass ``RecordingAuditSink``, which runs the same submission and record
validators atlas-audit runs, so a malformed record fails a unit test instead of being refused on the Pi.

Records carry resource IDs, sizes and hashes, never content (D7). Free text a tool was given is
recorded as ``{"sha256": ..., "bytes": ...}``; ``$VAR`` names, enums, numbers and booleans as is.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

from atlas.audit.client import AppendReply, AuditError
from atlas.audit.record import ZERO_HASH, is_durable, record_hash, validate_record, validate_submission
from atlas.core.taint import VAR_NAME_RE
from atlas.policy.types import Context

Json = dict[str, Any]
TOOL_NAME_MAX: Final = 64


class AuditSink(Protocol):
    async def append(self, record: dict[str, Any]) -> AppendReply: ...


def text_digest(text: str) -> Json:
    raw = text.encode("utf-8", "surrogatepass")
    return {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def redact_args(args: Mapping[str, Any]) -> Json:
    """``args_redacted``: free text hashed, ``$VAR`` names and scalars kept, depth bounded."""

    def walk(value: Any, depth: int) -> Any:
        if depth > 4:
            return "[TRUNCATED]"
        if isinstance(value, str):
            return value if VAR_NAME_RE.fullmatch(value) else text_digest(value)
        if isinstance(value, bool) or value is None or (isinstance(value, int) and abs(value) < 2**53):
            return value
        if isinstance(value, list):
            return [walk(v, depth + 1) for v in value[:20]]
        if isinstance(value, dict):
            return {str(k)[:64]: walk(v, depth + 1) for k, v in list(value.items())[:32]}
        return "[UNSUPPORTED]"

    return {str(k)[:64]: walk(v, 1) for k, v in list(args.items())[:32]}


def safe_tool_name(name: str) -> str:
    """The audit ``tool`` pattern; a model-invented name that doesn't fit is recorded as ``invalid_tool``."""
    if len(name) <= TOOL_NAME_MAX and re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*", name):
        return name
    return "invalid_tool"


@dataclass(frozen=True, slots=True)
class DataRef:
    resource_id: str
    bytes: int
    sha256: str
    tainted: bool

    def to_json(self) -> Json:
        return {"resource_id": self.resource_id, "bytes": self.bytes, "sha256": self.sha256, "tainted": self.tainted}


def tool_call_record(
    *,
    context: Context,
    correlation_id: str,
    tool: str,
    args: Mapping[str, Any],
    status: str,
    code: str | None = None,
    data_accessed: Sequence[DataRef] = (),
) -> Json:
    result: Json = {"status": status}
    if code is not None:
        result["code"] = code
    return {
        "kind": "tool_call",
        "context": context.value,
        "correlation_id": correlation_id,
        "tool": safe_tool_name(tool),
        "args_redacted": redact_args(args),
        "data_accessed": [d.to_json() for d in data_accessed[:256]],
        "result": result,
    }


def action_record(
    kind: str,
    *,
    context: Context,
    action_id: str,
    action_kind: str,
    payload_sha256: str,
    tool: str | None = None,
    correlation_id: str | None = None,
    device_id: str | None = None,
    **action_fields: Any,
) -> Json:
    """``action_proposed``, ``approval``, ``write_ahead`` or ``write_result``."""
    action: Json = {"action_id": action_id, "action_kind": action_kind, "payload_sha256": payload_sha256}
    action.update({k: v for k, v in action_fields.items() if k != "result" and v is not None})
    record: Json = {"kind": kind, "context": context.value, "action": action}
    if tool is not None:
        record["tool"] = tool
    if correlation_id is not None:
        record["correlation_id"] = correlation_id
    if device_id is not None:
        record["device_id"] = device_id
    if "result" in action_fields:
        record["result"] = action_fields["result"]
    return record


class RecordingAuditSink:
    """An in-memory audit service for tests and the CLI. Validates exactly as atlas-audit does."""

    def __init__(self, actor: str, *, fail_with: str | None = None, durable: bool | None = None) -> None:
        self.actor = actor
        self.records: list[Json] = []
        self.fail_with = fail_with  # simulate an unavailable audit service
        self.force_durable = durable  # simulate a reply that lies about durability
        self._prev = ZERO_HASH
        self._seq = 0

    async def append(self, record: dict[str, Any]) -> AppendReply:
        if self.fail_with is not None:
            raise AuditError(self.fail_with)
        errors = validate_submission(record, self.actor)
        full = {**record, "v": 1, "actor": self.actor, "seq": self._seq + 1, "ts": 1, "prev_hash": self._prev}
        full["hash"] = record_hash(full)
        errors += validate_record(full)
        if errors:
            raise AssertionError(f"invalid audit record: {errors}")
        self._seq += 1
        self._prev = full["hash"]
        self.records.append(full)
        durable = is_durable(full["kind"], full) if self.force_durable is None else self.force_durable
        return AppendReply(seq=self._seq, hash=full["hash"], durable=durable)

    def of_kind(self, kind: str) -> list[Json]:
        return [r for r in self.records if r["kind"] == kind]
