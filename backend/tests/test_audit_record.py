"""JCS (RFC 8785) and the hand-written audit-record validator, against the golden chain and the schema."""

from __future__ import annotations

import copy
import json
import random
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from atlas.audit import jcs
from atlas.audit.record import (
    ACTORS,
    ZERO_HASH,
    record_hash,
    validate_record,
    validate_submission,
)
from tests.test_contracts import golden_chain  # reference implementation lives with the contracts
from tests.test_contracts import jcs as reference_jcs

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"
SCHEMA = json.loads((CONTRACTS / "audit-record.v1.schema.json").read_text(encoding="utf-8"))
VALIDATOR = Draft202012Validator(SCHEMA)


# --- JCS ---------------------------------------------------------------------------------------------


def test_jcs_reproduces_golden_chain_byte_for_byte() -> None:
    chain = golden_chain()
    assert len(chain) >= 8
    prev = ZERO_HASH
    for rec in chain:
        body = {k: v for k, v in rec.items() if k != "hash"}
        assert jcs.canonicalize(body) == reference_jcs(body)
        assert record_hash(rec) == rec["hash"], rec["seq"]
        assert rec["prev_hash"] == prev
        prev = rec["hash"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"b": 1, "a": [True, None, "x"]}, b'{"a":[true,null,"x"],"b":1}'),
        ('a\u0008\u0009\u000a\u000c\u000d\u0001\u001f"\\/\u007f', b'"a\\b\\t\\n\\f\\r\\u0001\\u001f\\"\\\\/\x7f"'),
        ("é€\U0001f600", '"é€\U0001f600"'.encode()),
        (0, b"0"),
        (-7, b"-7"),
        ([], b"[]"),
        ({}, b"{}"),
    ],
)
def test_jcs_basic_rules(value: Any, expected: bytes) -> None:
    assert jcs.canonicalize(value) == expected


def test_jcs_sorts_keys_by_utf16_code_units_not_code_points() -> None:
    # U+FF5E (BMP, UTF-16 0xFF5E) vs U+1F600 (astral, UTF-16 0xD83D 0xDE00): by code point the astral
    # character sorts last, by UTF-16 code units it sorts first (RFC 8785 section 3.2.3).
    out = jcs.canonicalize({"\uff5e": 1, "\U0001f600": 2})
    assert out == '{"\U0001f600":2,"\uff5e":1}'.encode()
    assert sorted(["\uff5e", "\U0001f600"]) == ["\uff5e", "\U0001f600"]  # Python's own order differs


@pytest.mark.parametrize("bad", [1.5, 1.0, float("nan"), 2**53, {"a": 0.1}, {1: 2}, b"bytes", {"k": {"n": [1.0]}}])
def test_jcs_rejects_what_it_cannot_canonicalize(bad: Any) -> None:
    with pytest.raises((TypeError, ValueError)):
        jcs.canonicalize(bad)


# --- validator vs the real schema --------------------------------------------------------------------


def schema_ok(record: Any) -> bool:
    return not list(VALIDATOR.iter_errors(record))


def test_validator_accepts_every_golden_record() -> None:
    for rec in golden_chain():
        assert validate_record(rec) == [], rec["seq"]
        assert schema_ok(rec)


def test_actors_match_schema_enum() -> None:
    assert set(SCHEMA["properties"]["actor"]["enum"]) == ACTORS


def _mutations(rec: dict[str, Any], rng: random.Random) -> dict[str, Any]:
    out = copy.deepcopy(rec)
    choice = rng.randrange(7)
    if choice == 0:
        out["kind"] = rng.choice(SCHEMA["properties"]["kind"]["enum"])
    elif choice == 1:
        out["actor"] = rng.choice(SCHEMA["properties"]["actor"]["enum"])
    elif choice == 2:
        out["context"] = rng.choice([*SCHEMA["properties"]["context"]["enum"], "bogus"])
    elif choice == 3:
        for name in rng.sample([k for k in out if k not in ("v", "seq", "ts")], k=1):
            del out[name]
    elif choice == 4:
        out[rng.choice(["tool", "action", "egress", "power", "auth", "config", "system", "purge", "genesis"])] = {
            "event": "x"
        }
    elif choice == 5:
        out["extra"] = 1
    else:
        out["ts"] = rng.choice([-1, "1", True, 5])
    return out


def test_validator_agrees_with_schema_on_mutations() -> None:
    rng = random.Random(1234)  # noqa: S311  (test data, not crypto)
    chain = golden_chain()
    disagreements: list[Any] = []
    for _ in range(1500):
        rec = _mutations(rng.choice(chain), rng)
        if rng.random() < 0.3:
            rec = _mutations(rec, rng)
        if (validate_record(rec) == []) != schema_ok(rec):
            disagreements.append(rec)
    assert not disagreements, disagreements[:2]


def test_validator_rejects_floats_that_the_schema_would_call_integers() -> None:
    # jsonschema treats 1.0 as an integer; we refuse every float because JCS cannot represent it.
    rec = copy.deepcopy(golden_chain()[1])
    rec["args_redacted"] = {"limit": 1.0}
    assert validate_record(rec) != []


def test_kind_actor_rules() -> None:
    chain = golden_chain()
    write_ahead = next(r for r in chain if r["kind"] == "write_ahead")
    assert validate_record({**write_ahead, "actor": "atlas-core"}) != []
    assert not schema_ok({**write_ahead, "actor": "atlas-core"})


# --- submissions -------------------------------------------------------------------------------------


def test_submission_actor_spoofing_is_rejected() -> None:
    body = {"kind": "tool_call", "actor": "atlas-core"}
    assert "ACTOR_MISMATCH" in validate_submission(body, "atlas-panels")
    assert "ACTOR_MISMATCH" not in validate_submission(body, "atlas-core")


@pytest.mark.parametrize("field", ["seq", "ts", "prev_hash", "hash"])
def test_submission_cannot_set_server_assigned_fields(field: str) -> None:
    assert validate_submission({"kind": "tool_call", field: 1}, "atlas-core") != []


@pytest.mark.parametrize("kind", ["genesis", "purge", "checkpoint"])
def test_submission_cannot_create_internal_kinds(kind: str) -> None:
    assert validate_submission({"kind": kind}, "atlas-audit") != []


@pytest.mark.parametrize(
    ("kind", "wrong", "right"),
    [
        ("approval", "atlas-executor", "atlas-core"),
        ("write_ahead", "atlas-core", "atlas-executor"),
        ("write_result", "atlas-google", "atlas-executor"),
        ("egress", "atlas-google", "atlas-egress"),
    ],
)
def test_submission_per_kind_actor_constraints(kind: str, wrong: str, right: str) -> None:
    assert validate_submission({"kind": kind}, wrong) != []
    assert validate_submission({"kind": kind}, right) == []
