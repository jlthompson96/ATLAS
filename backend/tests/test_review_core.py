"""Regression tests for the security-core review (2026-11-18, at 3b8e151).

Each section names the finding. The reviewer's reproduction scripts (exp1..exp5) are rebuilt here as
tests that failed before the fix and pass after it.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Callable, Coroutine
from typing import Any

import pytest

from atlas.audit.client import AppendReply
from atlas.core import reader as reader_module
from atlas.core.auditing import RecordingAuditSink
from atlas.core.fake_llm import ScriptedBackend, call_turn, text_turn
from atlas.core.reader import QuarantinedReader, ReaderRefused, ReaderTask
from atlas.core.sanitize import MAX_HTML_BYTES, ContentFlag, sanitize
from atlas.core.taint import Source, SourceKind, UntrustedContent, VariableStore
from atlas.llm.interface import Message, Role, ToolSpec
from atlas.policy.types import ActionState, DecisionChannel
from tests.core_support import MOCK_PIN_TOKEN, Harness


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


# --- F1: the HTML sanitizer is linear and runs off the event loop inside the deadline ------------------------

WORST_CASES: dict[str, Callable[[int], bytes]] = {
    "nested_then_blocks": lambda n: b"<b>" * n + b"<p>x" * n,
    "open_then_unmatched_end_tags": lambda n: b"<i>" * n + b"</x>" * n,
    "hidden_nesting": lambda n: b"<div style='display:none'>" * n + b"x</div>" * n,
    "unclosed_comments_and_tags": lambda n: b"<p a=" * n + b"<!--" * n,
}


def _time(raw: bytes) -> float:
    best = float("inf")
    for _ in range(3):
        t = time.perf_counter()
        sanitize(raw, "text/html")
        best = min(best, time.perf_counter() - t)
    return best


@pytest.mark.parametrize("shape", sorted(WORST_CASES))
def test_f1_worst_case_html_scales_linearly(shape: str) -> None:
    build = WORST_CASES[shape]
    unit = len(build(1))
    n = (MAX_HTML_BYTES // 2) // unit
    small, large = _time(build(n // 2)), _time(build(n))
    # Doubling the input roughly doubles the time (quadratic would be 4x). Generous bound for CI noise.
    assert large < 3.0 * small + 0.02, (shape, small, large)


@pytest.mark.parametrize("shape", sorted(WORST_CASES))
def test_f1_worst_case_at_the_cap_finishes_fast(shape: str) -> None:
    raw = WORST_CASES[shape](MAX_HTML_BYTES)[: MAX_HTML_BYTES * 4]
    clean_time = _time(raw)
    assert clean_time < 1.0, clean_time


def test_f1_html_has_a_tighter_raw_cap_and_deep_nesting_is_hidden() -> None:
    clean = sanitize(b"<p>" + b"a" * (MAX_HTML_BYTES * 2), "text/html")
    assert clean.raw_bytes == MAX_HTML_BYTES and ContentFlag.TRUNCATED in clean.flags
    deep = sanitize(b"<p>visible</p>" + b"<span>" * 300 + b"DEEP", "text/html")
    assert "visible" in deep.text and "DEEP" not in deep.text
    assert ContentFlag.HIDDEN_HTML in deep.flags


def test_f1_an_end_tag_only_closes_a_recent_element() -> None:
    # A hidden element followed by >32 unclosed elements: its end tag can no longer reveal the rest.
    html = b"<div style='display:none'>" + b"<b>" * 40 + b"</div>AFTER"
    assert "AFTER" not in sanitize(html, "text/html").text


def _reader_for(
    item: UntrustedContent, monotonic: Callable[[], float] = time.monotonic
) -> tuple[QuarantinedReader, VariableStore, Any]:
    store = VariableStore(lambda: 1)
    ref = store.bind_item("PAGE", item, "web page")
    good = {"summary": "s", "category": "other", "urgency": "low", "asks_for_action": False, "contains_links": False}
    return QuarantinedReader(ScriptedBackend([text_turn(json.dumps(good))]), monotonic=monotonic), store, ref


def test_f1_sanitizing_does_not_block_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    real = reader_module.build_prompt
    on_thread: list[bool] = []

    def slow(*args: Any, **kwargs: Any) -> Any:
        on_thread.append(threading.current_thread() is not threading.main_thread())
        time.sleep(0.2)
        return real(*args, **kwargs)

    monkeypatch.setattr(reader_module, "build_prompt", slow)
    reader, store, ref = _reader_for(
        UntrustedContent(b"<p>hi</p>", source=Source(SourceKind.WEB, "web:src/1"), media_type="text/html")
    )

    async def go() -> int:
        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        task = asyncio.create_task(ticker())
        await reader.read(store, ref, ReaderTask.SUMMARIZE, deadline=time.monotonic() + 5)
        task.cancel()
        return ticks

    assert run(go()) >= 5  # the loop kept running while the page was sanitized
    assert on_thread == [True]


def test_f1_sanitizing_is_bounded_by_the_request_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reader_module, "build_prompt", lambda *a, **k: time.sleep(1.0))
    reader, store, ref = _reader_for(UntrustedContent(b"x", source=Source(SourceKind.WEB, "web:src/2")))
    with pytest.raises(ReaderRefused) as err:
        run(reader.read(store, ref, ReaderTask.SUMMARIZE, deadline=time.monotonic() + 0.05))
    assert err.value.code == "READER_TIMEOUT"
    with pytest.raises(ReaderRefused) as err:
        run(reader.read(store, ref, ReaderTask.SUMMARIZE, deadline=time.monotonic() - 1))
    assert err.value.code == "READER_TIMEOUT"


# --- F10: more inline hiding --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "style",
    [
        "opacity:0.05",
        "opacity: .09",
        "opacity:5%",
        "font-size:0.5px",
        "font-size:1px",
        "font-size:0.1em",
        "font-size:0.15rem",
        "mso-hide:all",
        "height:1px;overflow:hidden",
        "width:1px; overflow:hidden",
        "max-height:0",
        "left:-500px;position:absolute",
        "top:-1000px",
        "text-indent:-9999px",
        "color:#fefefe;background-color:#ffffff",
        "color:#010101;background:#000",
        "color:rgb(250,250,250);background-color:white",
        "color:rgba(0,0,0,0)",
        "color:#12345600",
    ],
)
def test_f10_more_inline_hiding_is_removed_and_flagged(style: str) -> None:
    clean = sanitize(f"<p>visible</p><span style='{style}'>SECRET</span>".encode(), "text/html")
    assert "SECRET" not in clean.text, style
    assert ContentFlag.HIDDEN_HTML in clean.flags


@pytest.mark.parametrize(
    "style",
    [
        "opacity:0.5",
        "font-size:12px",
        "font-size:0.9em",
        "height:20px;overflow:hidden",
        "color:#333;background:#fff",
        "left:-10px",
        "color:black",
    ],
)
def test_f10_ordinary_styles_stay_visible(style: str) -> None:
    clean = sanitize(f"<p>visible</p><span style='{style}'>SHOWN</span>".encode(), "text/html")
    assert "SHOWN" in clean.text, style
    assert ContentFlag.HIDDEN_HTML not in clean.flags


# --- F5: concurrent decisions on one action (exp3, exp3b) ------------------------------------------------------

PROPOSE_DANA = (
    "propose_action",
    {"kind": "email.send", "summary": "Hi Dana", "to": ["dana@example.org"], "subject": "Hi", "body": ["See you"]},
)


def _proposer(messages: list[Message], tools: list[ToolSpec] | None, schema: Any) -> Any:
    return text_turn("Proposed.") if messages[-1].role is Role.TOOL else call_turn(PROPOSE_DANA)


class SlowAudit:
    """Yields to the event loop on every append, as a real socket round trip does."""

    def __init__(self, inner: RecordingAuditSink) -> None:
        self.inner = inner

    async def append(self, record: dict[str, Any]) -> AppendReply:
        await asyncio.sleep(0)
        return await self.inner.append(record)


def _race_harness() -> tuple[Harness, str, dict[str, Any]]:
    h = Harness(backend=ScriptedBackend(responder=_proposer))
    out = run(h.run("email dana"))
    action_id = out.pending_actions[0]
    h.approvals._audit = SlowAudit(h.core_audit)  # type: ignore[assignment]
    h.stepups.register("hud-remote", MOCK_PIN_TOKEN, h.clock.wall() + 60_000)
    sha = h.runtime.pending.get(action_id).payload_sha256  # type: ignore[union-attr]
    kw = {
        "channel": DecisionChannel.HUD,
        "device_id": "hud-remote",
        "action_id": action_id,
        "payload_sha256": sha,
        "step_up_token": MOCK_PIN_TOKEN,
    }
    return h, action_id, kw


@pytest.mark.parametrize("order", [("approve", "deny"), ("deny", "approve"), ("approve", "approve")])
def test_f5_exactly_one_concurrent_decision_wins(order: tuple[str, str]) -> None:
    h, action_id, kw = _race_harness()

    async def both() -> list[Any]:
        return list(await asyncio.gather(*(h.approvals.decide(decision=d, **kw) for d in order)))

    first, second = run(both())
    assert (first.ok, second.ok) == (True, False)
    assert second.code == "BUSY" and second.signed is None
    action = h.runtime.pending.get(action_id)
    assert action is not None
    winner = order[0]
    assert action.state is (ActionState.APPROVED if winner == "approve" else ActionState.DENIED)
    # Audit trail and events agree: one decision recorded, one resolution emitted.
    assert [r["action"]["decision"] for r in h.core_audit.of_kind("approval")] == [winner]
    resolved = [e["outcome"] for e in h.events.of_type("action.resolved")]
    assert resolved == ["approved" if winner == "approve" else "denied"]
    # At most one send, and none at all when the deny won.
    if first.signed is not None:
        assert run(h.approvals.execute(first.signed, h.link))["ok"] is True
    assert len(h.email.sent) == (1 if winner == "approve" else 0)


def test_f5_a_failed_audit_returns_the_action_to_pending() -> None:
    h, action_id, kw = _race_harness()
    h.approvals._audit = RecordingAuditSink("atlas-core", fail_with="AUDIT_UNAVAILABLE")
    assert run(h.approvals.decide(decision="approve", **kw)).code == "BUSY"
    assert h.runtime.pending.get(action_id).state is ActionState.PENDING  # type: ignore[union-attr]


# --- single-use step-up (owner decision) ------------------------------------------------------------------------


def test_step_up_token_is_spent_by_a_successful_decision() -> None:
    h = Harness(backend=ScriptedBackend(responder=_proposer))
    first = run(h.run("email dana")).pending_actions[0]
    second = run(h.run("email dana again")).pending_actions[0]
    h.stepups.register("hud-remote", MOCK_PIN_TOKEN, h.clock.wall() + 60_000)
    sha = {a: h.runtime.pending.get(a).payload_sha256 for a in (first, second)}  # type: ignore[union-attr]
    kw = {"channel": DecisionChannel.HUD, "device_id": "hud-remote", "step_up_token": MOCK_PIN_TOKEN}
    # A refused attempt does not spend the token...
    assert (
        run(h.approvals.decide(action_id=first, decision="approve", payload_sha256="0" * 64, **kw)).code
        == "HASH_MISMATCH"
    )
    assert run(h.approvals.decide(action_id=first, decision="approve", payload_sha256=sha[first], **kw)).ok
    # ...but a successful one does: the same PIN entry cannot approve a second action within its 60 s.
    result = run(h.approvals.decide(action_id=second, decision="approve", payload_sha256=sha[second], **kw))
    assert result.code == "STEP_UP_REQUIRED"


def test_step_up_token_can_be_bound_to_the_action_on_screen() -> None:
    h = Harness(backend=ScriptedBackend(responder=_proposer))
    first = run(h.run("email dana")).pending_actions[0]
    second = run(h.run("email dana again")).pending_actions[0]
    h.stepups.register("hud-remote", MOCK_PIN_TOKEN, h.clock.wall() + 60_000, action_id=first)
    sha = {a: h.runtime.pending.get(a).payload_sha256 for a in (first, second)}  # type: ignore[union-attr]
    kw = {
        "channel": DecisionChannel.HUD,
        "device_id": "hud-remote",
        "step_up_token": MOCK_PIN_TOKEN,
        "decision": "approve",
    }
    assert run(h.approvals.decide(action_id=second, payload_sha256=sha[second], **kw)).code == "STEP_UP_REQUIRED"
    assert run(h.approvals.decide(action_id=first, payload_sha256=sha[first], **kw)).ok
