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
    EVENT_ACTORS,
    KIND_ACTORS,
    SERVICE_CREDENTIALS,
    ZERO_HASH,
    actor_errors,
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
        ("tool_call", "atlas-discord", "atlas-core"),
        ("action_proposed", "atlas-research", "atlas-core"),
        ("power", "atlas-ui", "atlas-core"),
        ("config", "atlas-google", "atlas-ui"),
        ("config", "atlas-panels", "atlas-core"),
    ],
)
def test_submission_per_kind_actor_constraints(kind: str, wrong: str, right: str) -> None:
    assert validate_submission({"kind": kind}, wrong) != []
    assert validate_submission({"kind": kind}, right) == []


# --- actor pinning (rev 1.2.0, proposal 2026-11-22-audit-actor-pinning) ------------------------------


def _record(kind: str, actor: str, sub: dict[str, Any] | None = None) -> dict[str, Any]:
    """A schema-valid record of ``kind`` apart from the actor pin, built on the golden power record."""
    rec = {k: v for k, v in golden_chain()[7].items() if k != "power"}
    rec.update(kind=kind, actor=actor, result={"status": "ok"})
    if kind == "power":
        rec["power"] = sub or {"event": "wake_ready"}
    elif kind == "config":
        rec["config"] = {"key": "privacy.timeout", "new_sha256": "a" * 64}
    elif kind in ("auth", "system"):
        rec[kind] = sub or {"event": "service_start"}
    return rec


def _auth_and_system_cases() -> list[tuple[str, dict[str, Any]]]:
    defs = SCHEMA["$defs"]
    cases: list[tuple[str, dict[str, Any]]] = []
    for kind in ("auth", "system"):
        for event in defs[kind.capitalize()]["properties"]["event"]["enum"]:
            cases.append((kind, {"event": event}))
    every_credential = sorted(set().union(*SERVICE_CREDENTIALS.values()))
    cases += [("auth", {"event": "credential_rotated", "subject": c}) for c in every_credential]
    return cases


def test_validator_and_schema_agree_on_every_actor_for_every_pinned_kind_and_event() -> None:
    disagreements = []
    for actor in sorted(ACTORS):
        recs = [_record(kind, actor) for kind in ("power", "config")]
        recs += [_record(kind, actor, sub) for kind, sub in _auth_and_system_cases()]
        for rec in recs:
            if (validate_record(rec) == []) != schema_ok(rec):
                disagreements.append((actor, rec["kind"], rec.get(rec["kind"])))
    assert not disagreements, disagreements[:5]


@pytest.mark.parametrize(
    ("kind", "event", "right", "wrong"),
    [
        ("auth", "device_paired", "atlas-ui", "atlas-discord"),
        ("auth", "stepup_verified", "atlas-ui", "atlas-core"),
        ("auth", "stepup_failed", "atlas-ui", "atlas-research"),
        ("auth", "portfolio_unlocked", "atlas-market", "atlas-ui"),
        ("auth", "oauth_renewed", "atlas-google", "atlas-discord"),
        ("auth", "oauth_expired", "atlas-executor", "atlas-core"),
        ("system", "killswitch", "atlas-core", "atlas-discord"),
        ("system", "task_killed", "atlas-core", "atlas-research"),
        ("system", "chain_verified", "atlas-audit", "atlas-google"),
        ("system", "chain_broken", "atlas-audit", "atlas-core"),
        ("system", "retention_run", "atlas-audit", "atlas-panels"),
    ],
)
def test_event_pins(kind: str, event: str, right: str, wrong: str) -> None:
    assert validate_record(_record(kind, right, {"event": event})) == []
    assert schema_ok(_record(kind, right, {"event": event}))
    assert validate_record(_record(kind, wrong, {"event": event})) != []
    assert not schema_ok(_record(kind, wrong, {"event": event}))
    body = {"kind": kind, "context": "system", kind: {"event": event}}
    assert validate_submission(body, right) == []
    assert any("may only be written by" in e for e in validate_submission(body, wrong))


@pytest.mark.parametrize("event", ["service_start", "service_stop", "error_forwarded"])
def test_self_describing_system_events_stay_open_to_every_service(event: str) -> None:
    for actor in ACTORS:
        assert validate_record(_record("system", actor, {"event": event})) == [], actor


def test_credential_rotated_only_for_the_writers_own_credentials() -> None:
    def rotated(actor: str, **auth: Any) -> dict[str, Any]:
        return _record("auth", actor, {"event": "credential_rotated", **auth})

    assert validate_record(rotated("atlas-discord", subject="discord-reader-bot-token")) == []
    assert validate_record(rotated("atlas-executor", subject="google-oauth-client")) == []  # shared name, own copy
    assert validate_record(rotated("atlas-google", subject="google-oauth-client")) == []
    for bad in (
        rotated("atlas-discord", subject="discord-poster-bot-token"),  # the executor's
        rotated("atlas-panels", subject="twelvedata-api-key"),  # a service with no credentials
        rotated("atlas-ui", subject="audit-checkpoint-key"),
        rotated("atlas-ui"),  # no subject
    ):
        assert validate_record(bad) != [], bad
        assert not schema_ok(bad), bad


def test_pin_checks_never_raise_on_garbage() -> None:
    for junk in ([1], {"a": 1}, None, 5, True):
        for kind in ("auth", "system"):
            assert isinstance(actor_errors(kind, {"kind": kind, kind: {"event": junk}}, junk), list)
        rec = {"kind": "auth", "auth": {"event": "credential_rotated", "subject": junk}}
        assert actor_errors("auth", rec, "atlas-ui") != []
        assert actor_errors("auth", rec, junk) != []
        assert validate_submission({"kind": "auth", "auth": junk}, "atlas-ui") == []


def test_pin_tables_match_the_schema() -> None:
    pinned_kinds = {}
    event_pins: dict[str, dict[str, frozenset[str]]] = {"auth": {}, "system": {}}
    credential_lists: dict[str, frozenset[str]] = {}
    for branch in SCHEMA["allOf"]:
        cond = branch["if"]["properties"]
        kind = cond["kind"]["const"]
        then = branch["then"]
        actor = then.get("properties", {}).get("actor")
        if kind in event_pins and kind in cond:
            event = cond[kind]["properties"]["event"]
            if "anyOf" in then:  # credential_rotated
                for alt in then["anyOf"]:
                    props = alt["properties"]
                    credential_lists[props["actor"]["const"]] = frozenset(
                        props["auth"]["properties"]["subject"]["enum"]
                    )
                continue
            allowed = frozenset(actor["enum"] if "enum" in actor else [actor["const"]])
            for name in event["enum"]:
                event_pins[kind][name] = allowed
        elif actor is not None:
            pinned_kinds[kind] = frozenset(actor["enum"] if "enum" in actor else [actor["const"]])
    assert pinned_kinds == KIND_ACTORS
    assert event_pins == EVENT_ACTORS
    assert credential_lists == SERVICE_CREDENTIALS


def test_service_credentials_match_services_md() -> None:
    from tests.test_deploy_structure import NON_AUDIT, md_credentials

    # atlas-backup holds a credential but never writes audit records, so it can't log rotating it (ADR 0002)
    declared = {s: frozenset(names) for s, names in md_credentials().items() if names and s not in NON_AUDIT}
    assert md_credentials()["atlas-backup"] == {"backup-repo-password"}
    assert declared == SERVICE_CREDENTIALS
