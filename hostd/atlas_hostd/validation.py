"""Strict request-body validation, mirroring the closed schemas in contracts/hostd.v1.yaml.

Error messages name the offending field but never echo values (no content from the wire reaches logs or replies).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from atlas_hostd.leases import TTL_MAX_S, TTL_MIN_S
from atlas_hostd.models import (
    ID_PATTERN,
    HelperReport,
    LeaseReason,
    LeaseRequest,
    SleepRequest,
    SleepRequestReason,
    WakeClaim,
)

_MAX_INT = 2**31 - 1
# RFC 3339 date-time with an explicit offset; fractional seconds allowed.
_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]\d{2}:\d{2})$")


class ValidationError(ValueError):
    """The body does not satisfy the contract schema."""


def parse_json_object(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValidationError("body is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValidationError("body must be a JSON object")
    return value


def _closed(obj: Mapping[str, Any], required: set[str]) -> None:
    missing = required - set(obj)
    if missing:
        raise ValidationError(f"missing field: {sorted(missing)[0]}")
    extra = set(obj) - required
    if extra:
        raise ValidationError(f"unknown field: {sorted(extra)[0]}")


def _id(value: Any, name: str) -> str:
    if not isinstance(value, str) or ID_PATTERN.fullmatch(value) is None:
        raise ValidationError(f"{name} is not a valid id")
    return value


def _int(value: Any, name: str, *, lo: int = 0, hi: int = _MAX_INT) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ValidationError(f"{name} must be an integer in {lo}..{hi}")
    return value


def _bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValidationError(f"{name} must be a boolean")
    return value


def _enum[E: StrEnum](value: Any, name: str, enum_type: type[E]) -> E:
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be a string")
    try:
        return enum_type(value)
    except ValueError as exc:
        raise ValidationError(f"{name} is not an allowed value") from exc


def parse_timestamp(value: Any, name: str) -> datetime:
    if not isinstance(value, str) or _RFC3339.fullmatch(value) is None:
        raise ValidationError(f"{name} must be an RFC 3339 date-time")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"{name} must be an RFC 3339 date-time") from exc
    return parsed.astimezone(UTC)


def parse_sleep_request(body: bytes) -> SleepRequest:
    obj = parse_json_object(body)
    _closed(obj, {"request_id", "reason", "atlas_idle_s", "wake"})
    wake_raw = obj["wake"]
    claim: WakeClaim | None = None
    if wake_raw is not None:
        if not isinstance(wake_raw, dict):
            raise ValidationError("wake must be an object or null")
        _closed(wake_raw, {"wake_id", "first_packet_at"})
        claim = WakeClaim(
            wake_id=_id(wake_raw["wake_id"], "wake.wake_id"),
            first_packet_at=parse_timestamp(wake_raw["first_packet_at"], "wake.first_packet_at"),
        )
    return SleepRequest(
        request_id=_id(obj["request_id"], "request_id"),
        reason=_enum(obj["reason"], "reason", SleepRequestReason),
        atlas_idle_s=_int(obj["atlas_idle_s"], "atlas_idle_s"),
        wake=claim,
    )


def parse_lease_request(body: bytes) -> LeaseRequest:
    obj = parse_json_object(body)
    _closed(obj, {"ttl_s", "reason"})
    return LeaseRequest(
        ttl_s=_int(obj["ttl_s"], "ttl_s", lo=TTL_MIN_S, hi=TTL_MAX_S),
        reason=_enum(obj["reason"], "reason", LeaseReason),
    )


def parse_helper_report(body: bytes) -> HelperReport:
    obj = parse_json_object(body)
    _closed(obj, {"session_id", "input_idle_s", "fullscreen_app", "gpu_heavy_process_count", "reported_at"})
    return HelperReport(
        session_id=_int(obj["session_id"], "session_id"),
        input_idle_s=_int(obj["input_idle_s"], "input_idle_s"),
        fullscreen_app=_bool(obj["fullscreen_app"], "fullscreen_app"),
        gpu_heavy_process_count=_int(obj["gpu_heavy_process_count"], "gpu_heavy_process_count"),
        reported_at=parse_timestamp(obj["reported_at"], "reported_at"),
    )


def valid_id(value: str) -> bool:
    return ID_PATTERN.fullmatch(value) is not None
