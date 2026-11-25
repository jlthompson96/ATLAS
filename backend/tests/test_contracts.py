"""Contract checks: each contract is valid, accepts good examples, rejects bad ones, and agrees
with the other contracts it overlaps."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator
from openapi_spec_validator import validate as validate_openapi

from atlas.llm.interface import LLMBackend, ReadinessStage
from atlas.power.interface import PowerController, PowerState
from atlas.ui_server.protocol import Contract, ContractViolation, StreamChecker, contracts_dir, redact_for_log

CONTRACTS = contracts_dir()
ZERO_HASH = "0" * 64


def load_json(name: str) -> Any:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


EVENTS = Contract.load()
AUDIT = Draft202012Validator(load_json("audit-record.v1.schema.json"))


def frame(type_: str, payload: dict[str, Any], seq: int = 1) -> dict[str, Any]:
    return {"v": 1, "type": type_, "seq": seq, "ts": 1_763_279_000_000, "payload": payload}


# --- events.v1 -----------------------------------------------------------------------------------


def test_events_schema_is_valid_2020_12() -> None:
    Draft202012Validator.check_schema(load_json("events.v1.schema.json"))


@pytest.mark.parametrize(
    ("type_", "payload"),
    [
        ("state.changed", {"state": "idle"}),
        ("state.changed", {"state": "waking_host", "wake": {"stage": "api", "elapsed_ms": 9000, "deadline_ms": 60000}}),
        ("state.changed", {"state": "error", "code": "WAKE_FAILED", "detail": "core didn't wake"}),
        ("state.changed", {"state": "llm_unreachable", "code": "SIGNED_OUT"}),
        ("audio.level", {"stream": "tts", "t_play": 1_763_279_000_150.5, "frame_ms": 16, "levels": [0.1, 0.5]}),
        (
            "panel.update",
            {"panel_id": "email", "sensitivity": "secret", "redacted": True, "updated_at": 1, "data": {"unread": 3}},
        ),
        (
            "panel.update",
            {
                "panel_id": "portfolio",
                "sensitivity": "secret",
                "redacted": False,
                "updated_at": 1,
                "data": {"locked": True},
            },
        ),
        ("request.result", {"ref_seq": 4, "ok": False, "code": "BAD_PIN"}),
    ],
)
def test_events_accepts(type_: str, payload: dict[str, Any]) -> None:
    EVENTS.validate("server", frame(type_, payload))


@pytest.mark.parametrize(
    ("why", "type_", "payload"),
    [
        ("waking_host needs wake", "state.changed", {"state": "waking_host"}),
        (
            "wake only with waking_host",
            "state.changed",
            {"state": "idle", "wake": {"stage": "packet", "elapsed_ms": 0, "deadline_ms": 60000}},
        ),
        ("error needs code+detail", "state.changed", {"state": "error"}),
        ("unreachable code enum", "state.changed", {"state": "llm_unreachable", "code": "HOST_BUSY"}),
        ("unknown field", "state.changed", {"state": "idle", "colour": "red"}),
        ("frame_ms fixed", "audio.level", {"stream": "tts", "t_play": 1, "frame_ms": 20, "levels": [0.1]}),
        ("level range", "audio.level", {"stream": "mic", "t_play": 1, "frame_ms": 16, "levels": [1.5]}),
        (
            "email is secret",
            "panel.update",
            {"panel_id": "email", "sensitivity": "public", "redacted": True, "updated_at": 1, "data": {"unread": 3}},
        ),
        (
            "redacted email is count only",
            "panel.update",
            {
                "panel_id": "email",
                "sensitivity": "secret",
                "redacted": True,
                "updated_at": 1,
                "data": {"unread": 1, "auth": {"status": "ok"}, "items": []},
            },
        ),
        (
            "weather never redacted",
            "panel.update",
            {"panel_id": "weather", "sensitivity": "public", "redacted": True, "updated_at": 1, "data": {}},
        ),
        (
            "attribution required",
            "panel.update",
            {
                "panel_id": "weather",
                "sensitivity": "public",
                "redacted": False,
                "updated_at": 1,
                "data": {"temp_c": 1, "condition_code": 0, "condition": "Clear", "high_c": 2, "low_c": 0},
            },
        ),
        (
            "money is a string",
            "panel.update",
            {
                "panel_id": "portfolio",
                "sensitivity": "secret",
                "redacted": False,
                "updated_at": 1,
                "data": {
                    "locked": False,
                    "currency": "USD",
                    "total_value": 100.5,
                    "day_change": "1",
                    "day_change_pct": 1,
                    "as_of": 1,
                    "top_movers": [],
                },
            },
        ),
        (
            "redacted task has no title",
            "task.progress",
            {
                "task_id": "t1",
                "kind": "research",
                "status": "running",
                "title": "secret",
                "redacted": True,
                "step": 1,
                "budget": {"steps": 1, "time_s": 1, "tokens": 1, "bytes": 1},
                "used": {"steps": 1, "time_s": 1, "tokens": 1, "bytes": 1},
                "unattended": False,
            },
        ),
        ("failed result needs code", "request.result", {"ref_seq": 1, "ok": False}),
        ("client type from server", "clock.ping", {"client_t": 1}),
    ],
)
def test_events_rejects(why: str, type_: str, payload: dict[str, Any]) -> None:
    with pytest.raises(ContractViolation):
        EVENTS.validate("server", frame(type_, payload))


def test_client_messages() -> None:
    EVENTS.validate("client", frame("portfolio.unlock", {"passphrase": "correct horse"}))
    EVENTS.validate(
        "client",
        frame(
            "action.decide",
            {"action_id": "a1", "decision": "approve", "payload_sha256": "a" * 64, "step_up_token": "t" * 32},
        ),
    )
    with pytest.raises(ContractViolation):
        EVENTS.validate(
            "client", frame("action.decide", {"action_id": "a1", "decision": "approve", "payload_sha256": "a" * 64})
        )
    with pytest.raises(ContractViolation):
        EVENTS.validate("client", frame("state.changed", {"state": "idle"}))


def test_sensitive_fields_are_marked_and_redacted() -> None:
    assert {"passphrase", "pin", "step_up_token", "token"} <= EVENTS.sensitive_keys
    out = redact_for_log(frame("portfolio.unlock", {"passphrase": "hunter2"}))
    assert "hunter2" not in json.dumps(out)


def test_state_machine_rules() -> None:
    checker = StreamChecker(EVENTS)
    snapshot_state = {"state": {"state": "idle"}}
    checker.check({"seq": 0, "ts": 0, "type": "sync.snapshot", "payload": snapshot_state})
    with pytest.raises(ContractViolation, match="illegal state transition idle -> speaking"):
        checker.check({"seq": 1, "ts": 0, "type": "state.changed", "payload": {"state": "speaking"}})

    checker = StreamChecker(EVENTS)
    checker.check({"seq": 0, "ts": 0, "type": "sync.snapshot", "payload": snapshot_state})
    wake = {"state": "waking_host", "wake": {"stage": "api", "elapsed_ms": 5000, "deadline_ms": 60000}}
    checker.check({"seq": 1, "ts": 0, "type": "state.changed", "payload": wake})
    back = {"state": "waking_host", "wake": {"stage": "link", "elapsed_ms": 6000, "deadline_ms": 60000}}
    with pytest.raises(ContractViolation, match="backwards"):
        checker.check({"seq": 2, "ts": 0, "type": "state.changed", "payload": back})
    with pytest.raises(ContractViolation, match="seq"):
        checker.check({"seq": 1, "ts": 0, "type": "alert", "payload": {}})


def test_every_state_has_transitions_defined() -> None:
    states = set(load_json("events.v1.schema.json")["$defs"]["State"]["enum"])
    assert set(EVENTS.transitions) == states
    for targets in EVENTS.transitions.values():
        assert targets <= states


# --- audit-record.v1 ---------------------------------------------------------------------------


def jcs(record: dict[str, Any]) -> bytes:
    """RFC 8785 for audit records (no floats, so plain sorted compact JSON is exact)."""
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def record_hash(record: dict[str, Any]) -> str:
    """Reference implementation of the audit hash rule."""
    return hashlib.sha256(jcs({k: v for k, v in record.items() if k != "hash"})).hexdigest()


def verify_chain(records: list[dict[str, Any]]) -> None:
    prev = ZERO_HASH
    for i, rec in enumerate(records):
        assert rec["seq"] == i, f"gap at {i}"
        assert rec["prev_hash"] == prev, f"broken link at seq {i}"
        assert rec["hash"] == record_hash(rec), f"bad hash at seq {i}"
        prev = rec["hash"]


def golden_chain() -> list[dict[str, Any]]:
    path = CONTRACTS / "examples" / "audit-chain.v1.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_audit_schema_is_valid_2020_12() -> None:
    Draft202012Validator.check_schema(load_json("audit-record.v1.schema.json"))


def test_golden_audit_chain_validates_and_verifies() -> None:
    records = golden_chain()
    for rec in records:
        errors = [e.message for e in AUDIT.iter_errors(rec)]
        assert not errors, (rec["seq"], errors)
    verify_chain(records)
    kinds = {r["kind"] for r in records}
    assert {
        "genesis",
        "tool_call",
        "egress",
        "action_proposed",
        "approval",
        "write_ahead",
        "write_result",
        "power",
    } <= kinds


@pytest.mark.parametrize("mutation", ["modify", "delete", "reorder"])
def test_audit_tamper_is_detected(mutation: str) -> None:  # PT-13
    records = golden_chain()
    if mutation == "modify":
        records[3]["context"] = "interactive" if records[3]["context"] != "interactive" else "unattended"
    elif mutation == "delete":
        del records[2]
    else:
        records[2], records[3] = records[3], records[2]
    with pytest.raises(AssertionError):
        verify_chain(records)


def test_audit_rejects_floats_and_misplaced_fields() -> None:
    base = golden_chain()[1]
    bad_float = {**base, "args_redacted": {"limit": 2.5}}
    assert list(AUDIT.iter_errors(bad_float))
    bad_kind_object = {**base, "power": {"event": "wake_ready"}}  # power object on a tool_call
    assert list(AUDIT.iter_errors(bad_kind_object))
    bad_actor = {**golden_chain()[5], "actor": "atlas-core"}  # write_ahead must come from the executor
    assert list(AUDIT.iter_errors(bad_actor))


# --- hostd.v1 ------------------------------------------------------------------------------------


def test_hostd_openapi_is_valid() -> None:
    spec = yaml.safe_load((CONTRACTS / "hostd.v1.yaml").read_text(encoding="utf-8"))
    validate_openapi(spec)
    assert spec["servers"][0]["url"] == "http://10.77.0.2:8770"


# --- cross-contract consistency --------------------------------------------------------------------


def _services_md_pi_services() -> set[str]:
    text = (CONTRACTS / "services.md").read_text(encoding="utf-8")
    section = text.split("## 1. Services", 1)[1].split("## 2.", 1)[0]
    rows = re.findall(r"^\| `(atlas-[a-z-]+)` \| [^|]+ \| (Pi|Desktop) \|", section, flags=re.M)
    return {name for name, host in rows if host == "Pi"}


def test_audit_actors_match_services_md() -> None:
    actors = set(load_json("audit-record.v1.schema.json")["properties"]["actor"]["enum"])
    # the kiosk and the backup (no socket, no network; ADR 0002) never write audit records
    pi_services = _services_md_pi_services() - {"atlas-kiosk", "atlas-backup"}
    assert actors == pi_services


def test_interfaces_match_event_enums() -> None:
    defs = load_json("events.v1.schema.json")["$defs"]
    assert [s.value for s in ReadinessStage] == defs["WakeStage"]["enum"]
    power_enum = defs["HostStatus"]["properties"]["llm"]["properties"]["power"]["enum"]
    assert {s.value for s in PowerState} == set(power_enum)


def test_interfaces_are_abstract() -> None:
    with pytest.raises(TypeError):
        LLMBackend()  # type: ignore[abstract]
    with pytest.raises(TypeError):
        PowerController()  # type: ignore[abstract]


def test_contract_revisions_present() -> None:
    assert re.fullmatch(r"1\.\d+\.\d+", EVENTS.revision)
    assert re.fullmatch(r"1\.\d+\.\d+", load_json("audit-record.v1.schema.json")["x-atlas-schema-revision"])
    spec = yaml.safe_load((CONTRACTS / "hostd.v1.yaml").read_text(encoding="utf-8"))
    assert re.fullmatch(r"1\.\d+\.\d+", spec["info"]["version"])
    assert Path(CONTRACTS / "README.md").is_file()


def _client_free_text(defs: dict[str, Any]) -> list[tuple[str, bool]]:
    """(path, sensitive) for every free-text string a client sends: no pattern, enum or const."""
    out: list[tuple[str, bool]] = []

    def walk(node: Any, path: str, seen: frozenset[str]) -> None:
        if not isinstance(node, dict):
            return
        if "$ref" in node:
            name = node["$ref"].rsplit("/", 1)[-1]
            if name not in seen:
                walk(defs[name], name, seen | {name})
            return
        if node.get("type") == "string" and not {"pattern", "enum", "const"} & node.keys():
            out.append((path, node.get("x-atlas-sensitive") is True))
        for key, child in node.get("properties", {}).items():
            walk(child, f"{path}.{key}", seen)
        for combinator in ("anyOf", "oneOf", "allOf"):
            for child in node.get(combinator, []):
                walk(child, path, seen)
        for branch in ("then", "else", "items"):
            if branch in node:
                walk(node[branch], path, seen)

    walk(defs["ClientMessage"], "ClientMessage", frozenset({"ClientMessage"}))
    return out


def test_every_free_text_string_a_client_sends_is_sensitive() -> None:
    """contracts/README.md (events.v1 1.2.0): what a person types is never logged. Found 2026-11-25 (text.input)."""
    fields = _client_free_text(load_json("events.v1.schema.json")["$defs"])
    assert ("TextInput.text", True) in fields
    assert ("PortfolioUnlock.passphrase", True) in fields
    # Envelope.type is the message type, not free text: ClientMessage restricts it to the enum of client types.
    assert [path for path, sensitive in fields if not sensitive and path != "Envelope.type"] == []
