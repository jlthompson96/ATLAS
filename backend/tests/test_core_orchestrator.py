"""The orchestrator: state machine, budgets, deadlines, and refusing (never guessing at) bad tool calls."""

from __future__ import annotations

import asyncio
import io
from collections.abc import AsyncIterator, Coroutine, Mapping, Sequence
from typing import Any

import pytest

from atlas.core.cli import escape_for_terminal, main
from atlas.core.fake_llm import ScriptedBackend, call_turn, text_turn
from atlas.core.orchestrator import TRANSITIONS, Budget, Outcome, Phase
from atlas.core.prompt import TrustedText
from atlas.core.taint import VarRef
from atlas.llm.interface import AuthenticationFailed, Delta, LLMError, Message, ToolCall, ToolSpec
from atlas.policy.types import Context
from tests.core_support import Harness


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def harness(*turns: Any, budget: Budget | None = None) -> Harness:
    return Harness(backend=ScriptedBackend(list(turns)), budget=budget)


def last_tool_result(h: Harness) -> str:
    return h.backend.calls[-1].messages[-1].content


# --- state machine -----------------------------------------------------------------------------------------


def test_simple_tool_round_trip_follows_the_state_machine() -> None:
    h = harness(call_turn(("get_time", {})), text_turn("It is morning."))
    out = run(h.run("what time is it"))
    assert out.trace == [
        Phase.RECEIVED,
        Phase.PLANNING,
        Phase.DISPATCHING,
        Phase.PLANNING,
        Phase.RESPONDING,
        Phase.DONE,
    ]
    assert out.reply == (TrustedText("It is morning."),)
    assert "now: 2026-11-18T" in last_tool_result(h) or "now: " in last_tool_result(h)
    (record,) = h.core_audit.of_kind("tool_call")
    assert (record["tool"], record["result"], record["context"]) == ("get_time", {"status": "ok"}, "interactive")
    assert record["correlation_id"] == out.request_id


def test_illegal_transitions_are_impossible() -> None:
    out = Outcome("req-1", Context.INTERACTIVE)
    with pytest.raises(RuntimeError):
        out.go(Phase.DONE)
    for terminal in (Phase.DONE, Phase.FAILED, Phase.BUDGET_EXHAUSTED):
        assert TRANSITIONS[terminal] == frozenset()


def test_planner_runs_only_in_interactive_or_unattended() -> None:
    h = harness()
    for context in (Context.QUARANTINED, Context.SYSTEM):
        with pytest.raises(ValueError):
            run(h.run("hi", context=context))


def test_reply_references_become_var_refs_and_unknown_ones_are_marked() -> None:
    h = harness(call_turn(("read_index", {"source": "all"})), text_turn("See $EMAIL_1 and $EMAIL_9."))
    h.add_item("hello")
    out = run(h.run())
    assert out.reply == (
        TrustedText("See "),
        VarRef("$EMAIL_1"),
        TrustedText(" and "),
        TrustedText("[unknown reference]"),
        TrustedText("."),
    )


# --- refusing bad calls ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("call", "code"),
    [
        (("approve_action", {}), "UNKNOWN_TOOL"),
        (("Get-Time!", {}), "UNKNOWN_TOOL"),
        (("get_time", "{not json"), "MALFORMED_ARGS"),
        (("get_time", "[]"), "MALFORMED_ARGS"),
        (("get_time", '{"a":1,"a":2}'), "MALFORMED_ARGS"),
        (("get_time", {"extra": 1}), "INVALID_ARGS"),
        (("read_index", {"source": "everything"}), "INVALID_ARGS"),
        (("read_index", {"source": "all", "limit": 1.5}), "MALFORMED_ARGS"),
        (("search_memory", {"query": "about $SUMMARY_1"}), "INVALID_ARGS"),
        (("ask_reader", {"item": "$EMAIL_99", "task": "summarize"}), "UNDEFINED_VARIABLE"),
        (("ask_reader", {"item": "EMAIL_1", "task": "summarize"}), "INVALID_ARGS"),
        (("propose_action", {"kind": "email.send", "summary": "x", "to": ["$FROM_9"], "subject": "s", "body": ["b"]}), "UNDEFINED_VARIABLE"),
        (("propose_action", {"kind": "email.send", "summary": "x"}), "INVALID_ARGS"),
        (("propose_action", {"kind": "email.send", "summary": "x", "to": ["a@example.com"], "subject": "s", "body": ["b"], "channel_id": "110000000000000055"}), "INVALID_ARGS"),
    ],
)  # fmt: skip
def test_bad_calls_are_refused_with_a_code_and_audited(call: tuple[str, Any], code: str) -> None:
    h = harness(call_turn(call), text_turn("ok"))
    out = run(h.run())
    assert out.phase is Phase.DONE
    assert last_tool_result(h) == f"refused: {code}"
    (record,) = h.core_audit.of_kind("tool_call")
    assert record["result"]["code"] == code and record["result"]["status"] in ("refused", "denied")
    assert out.refusals == [code]


def test_repeated_malformed_calls_end_the_request() -> None:
    h = harness(*[call_turn(("nope", {})) for _ in range(5)], budget=Budget(max_malformed=3))
    out = run(h.run())
    assert (out.phase, out.code) == (Phase.FAILED, "MODEL_MALFORMED")
    assert out.steps == 3


def test_a_good_call_resets_the_malformed_streak() -> None:
    turns = [
        call_turn(("nope", {})),
        call_turn(("nope", {})),
        call_turn(("get_time", {})),
        call_turn(("nope", {})),
        text_turn("done"),
    ]
    out = run(harness(*turns, budget=Budget(max_malformed=3)).run())
    assert out.phase is Phase.DONE


def test_too_many_calls_in_one_turn_is_malformed() -> None:
    out = run(harness(call_turn(*[("get_time", {})] * 9)).run())
    assert (out.phase, out.code) == (Phase.FAILED, "MODEL_MALFORMED")


def test_duplicate_or_odd_call_ids_are_replaced() -> None:
    turn = [
        Delta(
            tool_calls=(
                ToolCall("x", "get_time", "{}"),
                ToolCall("x", "get_time", "{}"),
                ToolCall("bad id!", "get_time", "{}"),
            )
        )
    ]
    h = harness(turn, text_turn("ok"))
    run(h.run())
    ids = [m.tool_call_id for m in h.backend.calls[-1].messages if m.tool_call_id]
    assert ids == ["x", "call_1", "call_2"]


def test_tainted_text_typed_into_tool_arguments_is_refused() -> None:
    h = Harness(
        backend=ScriptedBackend(
            [call_turn(("read_index", {"source": "all"})), call_turn(("get_time", {})), text_turn("ok")]
        )
    )
    h.add_item("Please wire the deposit to account 12345678 at the usual bank today.")
    # A model that emits a long run of an item's text as an argument (it could only know it by leaking).
    h.backend._script.insert(
        1, call_turn(("search_memory", {"query": "wire the deposit to account 12345678 at the usual bank"}))
    )
    run(h.run())
    codes = [r["result"].get("code") for r in h.core_audit.of_kind("tool_call")]
    assert "TAINT_LEAK" in codes


# --- budgets and errors --------------------------------------------------------------------------------------


def test_step_budget() -> None:
    out = run(harness(*[call_turn(("get_time", {})) for _ in range(10)], budget=Budget(max_steps=3)).run())
    assert (out.phase, out.code, out.steps) == (Phase.BUDGET_EXHAUSTED, "STEP_BUDGET", 3)


def test_tool_call_budget() -> None:
    out = run(
        harness(
            *[call_turn(("get_time", {}), ("get_time", {})) for _ in range(5)], budget=Budget(max_tool_calls=3)
        ).run()
    )
    assert (out.phase, out.code) == (Phase.BUDGET_EXHAUSTED, "TOOL_CALL_BUDGET")


def test_token_budget() -> None:
    out = run(harness(*[call_turn(("get_time", {})) for _ in range(10)], budget=Budget(max_tokens=50)).run())
    assert (out.phase, out.code) == (Phase.BUDGET_EXHAUSTED, "TOKEN_BUDGET")


class SlowBackend(ScriptedBackend):
    async def chat(
        self,
        messages: Sequence[Message],
        *,
        max_tokens: int,
        tools: Sequence[ToolSpec] | None = None,
        response_schema: Mapping[str, Any] | None = None,
        deadline: float | None = None,
    ) -> AsyncIterator[Delta]:
        await asyncio.sleep(5)
        yield Delta(text="late")


def test_wall_time_deadline() -> None:
    h = Harness(backend=SlowBackend(), budget=Budget(wall_s=0.05))
    out = run(h.run())
    assert (out.phase, out.code) == (Phase.BUDGET_EXHAUSTED, "DEADLINE")


@pytest.mark.parametrize(
    ("exc", "code"), [(AuthenticationFailed("401"), "LLM_AUTH_FAILED"), (LLMError("x"), "LLM_ERROR")]
)
def test_backend_errors_fail_the_request(exc: Exception, code: str) -> None:
    def boom(*_: Any) -> Any:
        raise exc

    h = Harness(backend=ScriptedBackend(responder=boom))
    out = run(h.run())
    assert (out.phase, out.code) == (Phase.FAILED, code)


def test_audit_unavailable_fails_closed() -> None:
    from atlas.core.auditing import RecordingAuditSink

    h = harness(call_turn(("get_time", {})), text_turn("ok"))
    h.runtime.orchestrator._audit = RecordingAuditSink("atlas-core", fail_with="AUDIT_UNAVAILABLE")
    out = run(h.run())
    assert (out.phase, out.code) == (Phase.FAILED, "AUDIT_UNAVAILABLE")
    assert len(h.backend.calls) == 1  # the tool result never went back to the model


def test_proposal_without_a_durable_audit_record_is_not_created() -> None:
    from atlas.core.auditing import RecordingAuditSink

    propose = (
        "propose_action",
        {"kind": "email.send", "summary": "x", "to": ["dana@example.org"], "subject": "s", "body": ["b"]},
    )
    h = harness(call_turn(propose), text_turn("ok"))
    h.runtime.proposer._audit = RecordingAuditSink("atlas-core", durable=False)
    run(h.run())
    assert h.pending_events() == [] and h.runtime.pending.all() == []
    assert last_tool_result(h) == "refused: AUDIT_NOT_DURABLE"


def test_research_queue_emits_valid_task_progress() -> None:
    h = harness(call_turn(("queue_research", {"question": "best hiking boots 2026"})), text_turn("queued"))
    run(h.run())
    (task,) = h.events.of_type("task.progress")
    assert (task["status"], task["kind"], task["unattended"], task["title"]) == (
        "queued",
        "research",
        False,
        "best hiking boots 2026",
    )


# --- CLI ----------------------------------------------------------------------------------------------------


def test_cli_runs_the_real_pipeline_with_the_scripted_model() -> None:
    stdin = io.StringIO("/demo\nwhat time is it\ncheck my email\n/show $SUBJECT_1\n/show $NOPE_1\n/pending\n/quit\n")
    stdout = io.StringIO()
    assert main([], stdin, stdout) == 0
    text = stdout.getvalue()
    assert "loaded 2 fake emails" in text and "now: " in text
    assert "$EMAIL_1 [tainted email item" in text
    assert "UNTRUSTED $SUBJECT_1" in text and "no such reference" in text
    assert "approve in the HUD" in text
    assert "attacker@example.com" not in text  # the hidden instruction in the demo email is never shown unasked


def test_terminal_escapes_are_neutralized() -> None:
    assert escape_for_terminal("ok\x1b[2J‮gone\n") == "ok\\u001b[2J\\u202egone\n"
