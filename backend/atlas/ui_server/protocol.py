"""events.v1 helpers shared by the mock event server and the real atlas-ui server.

* ``Contract``: loads contracts/events.v1.schema.json and validates frames in each direction.
* ``StreamChecker``: rules a JSON Schema can't express (seq order, the §4.3 state machine,
  wake-stage order, the TTS ≥ 100 ms lead from §4.4).
* ``Sequencer``: builds server envelopes with per-connection ``seq``.
* ``redact_for_log``: strips every field the schema marks ``x-atlas-sensitive``.

Requires the ``mock`` extra (jsonschema).
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any, Final, Literal

from jsonschema import Draft202012Validator

SCHEMA_FILE: Final = "events.v1.schema.json"
TTS_MIN_LEAD_MS: Final = 100
WAKE_STAGES: Final = ("packet", "link", "api", "model")

Direction = Literal["server", "client"]
Json = dict[str, Any]


def contracts_dir() -> Path:
    """``$ATLAS_CONTRACTS_DIR`` or the repo's ``contracts/`` directory."""
    override = os.environ.get("ATLAS_CONTRACTS_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[3] / "contracts"


def now_ms() -> int:
    return time.time_ns() // 1_000_000


class ContractViolation(ValueError):
    """A frame or stream breaks events.v1."""


class Contract:
    def __init__(self, schema: Mapping[str, Any]) -> None:
        Draft202012Validator.check_schema(schema)
        self.schema = schema
        self.revision: str = schema["x-atlas-schema-revision"]
        defs = schema["$defs"]
        self.transitions: dict[str, frozenset[str]] = {
            k: frozenset(v) for k, v in defs["State"]["x-atlas-state-transitions"].items()
        }
        base = {k: v for k, v in schema.items() if k != "oneOf"}
        self._validators = {
            "server": Draft202012Validator({**base, "$ref": "#/$defs/ServerMessage"}),
            "client": Draft202012Validator({**base, "$ref": "#/$defs/ClientMessage"}),
        }
        self.sensitive_keys: frozenset[str] = frozenset(_sensitive_keys(schema))
        self.client_types: frozenset[str] = frozenset(
            t
            for part in defs["ClientMessage"]["allOf"]
            for t in part.get("properties", {}).get("type", {}).get("enum", [])
        )

    @classmethod
    @cache
    def load(cls) -> Contract:
        with (contracts_dir() / SCHEMA_FILE).open(encoding="utf-8") as f:
            return cls(json.load(f))

    def errors(self, direction: Direction, frame: Any) -> list[str]:
        validator = self._validators[direction]
        return [
            f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
            for e in sorted(validator.iter_errors(frame), key=lambda e: list(e.absolute_path))
        ]

    def validate(self, direction: Direction, frame: Any) -> None:
        errs = self.errors(direction, frame)
        if errs:
            kind = frame.get("type") if isinstance(frame, dict) else type(frame).__name__
            raise ContractViolation(f"{direction} frame {kind!r} invalid: " + "; ".join(errs[:5]))


def _sensitive_keys(node: Any, parent_key: str | None = None) -> Iterator[str]:
    if isinstance(node, dict):
        if node.get("x-atlas-sensitive") and parent_key:
            yield parent_key
        for key, value in node.items():
            if key == "properties" and isinstance(value, dict):
                for prop, sub in value.items():
                    yield from _sensitive_keys(sub, prop)
            else:
                yield from _sensitive_keys(value, None)
    elif isinstance(node, list):
        for item in node:
            yield from _sensitive_keys(item, None)


def redact_for_log(frame: Any, sensitive: frozenset[str] | None = None) -> Any:
    """Deep copy of ``frame`` with every sensitive field replaced. Use before any logging."""
    keys = Contract.load().sensitive_keys if sensitive is None else sensitive
    if isinstance(frame, dict):
        return {k: "[REDACTED]" if k in keys else redact_for_log(v, keys) for k, v in frame.items()}
    if isinstance(frame, list):
        return [redact_for_log(v, keys) for v in frame]
    return frame


@dataclass
class Sequencer:
    """Builds server envelopes for one connection."""

    next_seq: int = 0
    clock: Callable[[], int] = now_ms

    def envelope(self, type_: str, payload: Mapping[str, Any]) -> Json:
        frame = {"v": 1, "type": type_, "seq": self.next_seq, "ts": self.clock(), "payload": dict(payload)}
        self.next_seq += 1
        return frame


@dataclass
class StreamChecker:
    """Checks the server->client stream of one connection against rules beyond the schema."""

    contract: Contract
    last_seq: int | None = None
    state: str | None = None  # unknown until the first snapshot
    wake_stage_index: int = -1
    warnings: list[str] = field(default_factory=list)

    def check(self, frame: Mapping[str, Any]) -> None:
        seq = frame["seq"]
        if self.last_seq is not None and seq <= self.last_seq:
            raise ContractViolation(f"seq went from {self.last_seq} to {seq}")
        self.last_seq = seq
        type_, payload = frame["type"], frame["payload"]

        if type_ == "sync.snapshot":
            self.state = payload["state"]["state"]
            self.wake_stage_index = self._stage_index(payload["state"]) if self.state == "waking_host" else -1
            return

        if type_ == "state.changed":
            new = payload["state"]
            if self.state is None:
                raise ContractViolation("state.changed before any sync.snapshot")
            if new not in self.contract.transitions[self.state]:
                raise ContractViolation(f"illegal state transition {self.state} -> {new}")
            if new == "waking_host":
                idx = self._stage_index(payload)
                if self.state == "waking_host" and idx < self.wake_stage_index:
                    raise ContractViolation("wake stage went backwards")
                wake = payload["wake"]
                if wake["elapsed_ms"] > wake["deadline_ms"]:
                    raise ContractViolation("wake elapsed_ms exceeds deadline_ms")
                self.wake_stage_index = idx
            else:
                self.wake_stage_index = -1
            self.state = new
            return

        if type_ == "audio.level" and payload["stream"] == "tts":
            lead = payload["t_play"] - frame["ts"]
            if lead < TTS_MIN_LEAD_MS:
                self.warnings.append(f"seq {seq}: tts batch only {lead:.0f} ms ahead of playback")

    @staticmethod
    def _stage_index(state_payload: Mapping[str, Any]) -> int:
        return WAKE_STAGES.index(state_payload["wake"]["stage"])
