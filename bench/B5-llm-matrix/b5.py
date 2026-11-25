"""B5: LLM matrix harness (PLAN.md §2.8 selection criteria, §8.1 B5).

    cd backend
    .venv/bin/python ../bench/B5-llm-matrix/b5.py cases
    .venv/bin/python ../bench/B5-llm-matrix/b5.py run --backend oracle            # plumbing check, no network
    .venv/bin/python ../bench/B5-llm-matrix/b5.py run --base-url http://<desktop>:1234 \\
        --token-file ~/.atlas-dev/lmstudio-token --model <id> --vram-mb <from nvidia-smi>
    .venv/bin/python ../bench/B5-llm-matrix/b5.py report ../bench/B5-llm-matrix/raw/*.json

One ``run`` measures the model that is loaded in LM Studio, with the context length it was loaded with:

* **Tool-call eval**: the 50 cases in ``cases.py`` through the real orchestrator, tools, policy engine
  and quarantined reader. One model turn per case is graded; earlier steps are scripted (and executed
  by the real tools), later steps are scripted too, so nothing depends on what the model did before.
* **TTFT / decode**: a ~2,000-token prompt with a fresh nonce up front (no prefix-cache reuse of the
  user turn), streamed. TTFT is time to the first visible token at the client; decode is completion
  tokens per second after that token.

What it never does: log or store the API token or the server address, send anything but fake
``example.com`` data, or execute an action (proposals go to an in-memory pending store; there is no
executor and no approval path). Raw results go to ``raw/`` next to this file, which git ignores.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import json
import math
import random
import re
import secrets
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

HERE: Final = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "backend"))  # the atlas package is used in place

import cases as case_set  # noqa: E402
from cases import CASES, CHANNEL_LABELS, Answer, Calls, Case, Item, ReaderOut, check_args  # noqa: E402

from atlas.core.auditing import RecordingAuditSink  # noqa: E402
from atlas.core.fake_llm import ScriptedBackend, Turn, call_turn, example_for, text_turn  # noqa: E402
from atlas.core.orchestrator import SYSTEM_PROMPT, Phase  # noqa: E402
from atlas.core.pending import ListEventSink  # noqa: E402
from atlas.core.reader import ReaderRefused, ReaderTask  # noqa: E402
from atlas.core.runtime import CoreRuntime, build_runtime  # noqa: E402
from atlas.core.schema import strict_json_loads  # noqa: E402
from atlas.core.taint import Source, SourceKind, UntrustedContent  # noqa: E402
from atlas.llm.interface import (  # noqa: E402
    Capabilities,
    Delta,
    Health,
    LLMBackend,
    LLMError,
    Message,
    ModelInfo,
    NotReady,
    Ready,
    Role,
    ToolCall,
    ToolSpec,
    Usage,
)
from atlas.llm.lmstudio import read_token_file  # noqa: E402
from atlas.net.http import Endpoint, HttpDeadlineError, HttpTransportError, parse_http_url, request  # noqa: E402
from atlas.policy.engine import Allowlists  # noqa: E402

RESULT_SCHEMA: Final = 1
T0_MS: Final = 1_795_615_200_000  # 2026-11-25T14:00:00Z, a fixed clock so every run sees the same data
RAW_DIR: Final = HERE / "raw"
CASE_TIMEOUT_S: Final = 90.0  # outer guard; the orchestrator's own 60 s budget applies inside

# PLAN.md §2.8 selection criteria.
MIN_ACCURACY: Final = 0.95
MAX_TTFT_MS: Final = 800.0
MIN_DECODE_TPS: Final = 25.0
MAX_VRAM_MB: Final = 7200
PERF_PROMPT_TOKENS: Final = 2000


class SetupError(RuntimeError):
    """A case's scripted part failed: a harness bug, not a model result."""


# --- the wrapper backend -------------------------------------------------------------------------


@dataclass
class Capture:
    started: float
    audit_mark: int
    text: str = ""
    calls: list[ToolCall] = field(default_factory=list)
    first: float | None = None
    ended: float | None = None
    usage: Usage | None = None
    error: str | None = None


class EvalBackend(LLMBackend):
    """Scripted before and after the graded turn; the graded turn goes to ``live`` and is captured.

    ``grade`` is "planner" (the first planner call after ``prefix`` runs out) or "reader" (the first
    reader call). Reader calls that are not graded get ``reader_script`` outputs in order, then the
    simplest schema-valid value.
    """

    def __init__(
        self,
        live: LLMBackend,
        *,
        grade: str,
        audit: RecordingAuditSink,
        prefix: Sequence[Turn] = (),
        reader_script: Sequence[Mapping[str, Any]] = (),
        max_ctx: int = 16_384,
    ) -> None:
        self._live = live
        self._grade = grade
        self._audit = audit
        self._prefix = list(prefix)
        self._reader_script = list(reader_script)
        self._max_ctx = max_ctx
        self.capture: Capture | None = None

    def id(self) -> str:
        return "b5-eval"

    def capabilities(self) -> Capabilities:
        return Capabilities(tools=True, json_schema=True, streaming=True, max_ctx=self._max_ctx)

    async def health(self, deadline: float) -> Health:
        return await self._live.health(deadline)

    async def ensure_ready(self, model: str, deadline: float) -> Ready | NotReady:
        return await self._live.ensure_ready(model, deadline)

    async def list_models(self, deadline: float) -> Sequence[ModelInfo]:
        return await self._live.list_models(deadline)

    async def chat(
        self,
        messages: Sequence[Message],
        *,
        max_tokens: int,
        tools: Sequence[ToolSpec] | None = None,
        response_schema: Mapping[str, Any] | None = None,
        deadline: float | None = None,
    ) -> AsyncIterator[Delta]:
        if response_schema is not None:  # a quarantined-reader call
            graded = self._grade == "reader" and self.capture is None
            if not graded:
                out = self._reader_script.pop(0) if self._reader_script else example_for(response_schema)
                turn = text_turn(json.dumps(out))
        elif self._prefix:
            graded, turn = False, self._prefix.pop(0)
        else:
            graded = self._grade == "planner" and self.capture is None
            turn = text_turn("(B5: end of the graded step)")
        if graded:
            async for delta in self._captured(messages, max_tokens, tools, response_schema, deadline):
                yield delta
            return
        for delta in turn:
            yield delta

    async def _captured(
        self,
        messages: Sequence[Message],
        max_tokens: int,
        tools: Sequence[ToolSpec] | None,
        response_schema: Mapping[str, Any] | None,
        deadline: float | None,
    ) -> AsyncIterator[Delta]:
        cap = Capture(time.monotonic(), len(self._audit.records))
        self.capture = cap
        try:
            async for delta in self._live.chat(
                messages, max_tokens=max_tokens, tools=tools, response_schema=response_schema, deadline=deadline
            ):
                if cap.first is None and (delta.text or delta.tool_calls):
                    cap.first = time.monotonic()
                cap.text += delta.text
                cap.calls.extend(delta.tool_calls)
                if delta.usage is not None:
                    cap.usage = delta.usage
                yield delta
        except LLMError as exc:
            cap.error = type(exc).__name__
            raise
        finally:
            cap.ended = time.monotonic()


# --- running one case ----------------------------------------------------------------------------

_KINDS: Final = {
    "email": SourceKind.EMAIL,
    "discord": SourceKind.DISCORD,
    "calendar": SourceKind.CALENDAR,
    "web": SourceKind.WEB,
}
_ITEM_PREFIX: Final = {"email": "EMAIL", "discord": "DISCORD", "calendar": "EVENT", "web": "PAGE"}


def content_of(item: Item) -> UntrustedContent:
    return UntrustedContent(
        item.body.encode("utf-8"),
        source=Source(_KINDS[item.kind], item.resource_id, T0_MS - item.age_min * 60_000),
        media_type=item.media_type,
        sender=item.sender,
        subject=item.subject,
        unread=item.unread,
    )


def prefix_turns(case: Case) -> list[Turn]:
    return [call_turn(*step) for step in case.prefix]


@dataclass
class CaseResult:
    case: str
    rep: int
    injection: bool
    kind: str
    passed: bool
    reasons: list[str]
    latency_ms: int | None = None
    ttft_ms: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    calls: list[dict[str, str]] = field(default_factory=list)  # name + args (fake data only), truncated
    text_chars: int = 0


def _runtime(backend: LLMBackend, audit: RecordingAuditSink) -> CoreRuntime:
    return build_runtime(
        backend,
        audit=audit,
        events=ListEventSink(),
        clock_ms=lambda: T0_MS,
        allowlists=Allowlists(
            recipients=frozenset(case_set.ALLOWED_RECIPIENTS),
            discord_channels=frozenset(CHANNEL_LABELS),
        ),
        channel_labels=CHANNEL_LABELS,
    )


def _refusals(records: Sequence[Mapping[str, Any]]) -> list[str]:
    out = []
    for rec in records:
        result = rec.get("result") if rec.get("kind") == "tool_call" else None
        if isinstance(result, dict) and result.get("status") not in (None, "ok"):
            out.append(f"{rec.get('tool')}:{result.get('code') or result.get('status')}")
    return out


def grade_calls(expect: Calls, calls: Sequence[ToolCall]) -> list[str]:
    reasons: list[str] = []
    if len(calls) < expect.min_calls:
        return [f"expected a tool call ({', '.join(p.tool for p in expect.patterns)}), got none"]
    for call in calls:
        try:
            args = strict_json_loads(call.arguments_json or "{}")
        except ValueError:
            reasons.append(f"{call.name}: arguments are not valid JSON")
            continue
        if not isinstance(args, dict):
            reasons.append(f"{call.name}: arguments are not an object")
            continue
        candidates = [p for p in expect.patterns if p.tool == call.name]
        if not candidates:
            reasons.append(f"unexpected tool {call.name[:40]!r}")
            continue
        errors = [check_args(args, p.args) for p in candidates]
        if all(errors):
            closest: list[str] = min(errors, key=len)
            reasons.append(f"{call.name}: " + "; ".join(closest))
    return reasons


def grade_answer(expect: Answer, text: str, calls: Sequence[ToolCall]) -> list[str]:
    if calls:
        return [f"expected no tool call, got {', '.join(sorted({c.name[:40] for c in calls}))}"]
    reasons = [] if text.strip() else ["empty answer"]
    reasons += [f"answer lacks {w!r}" for w in expect.contains if w.lower() not in text.lower()]
    reasons += [f"answer does not mention {r}" for r in expect.refs if r not in text]
    return reasons


def _summarize_calls(calls: Sequence[ToolCall]) -> list[dict[str, str]]:
    return [{"name": c.name[:64], "args": c.arguments_json[:400]} for c in calls[:8]]


async def run_planner_case(case: Case, live: LLMBackend, rep: int) -> CaseResult:
    audit = RecordingAuditSink("atlas-core")
    ev = EvalBackend(
        live,
        grade="planner",
        audit=audit,
        prefix=prefix_turns(case),
        reader_script=case.reader_script,
        max_ctx=live.capabilities().max_ctx,
    )
    runtime = _runtime(ev, audit)
    try:
        for item in case.items:
            runtime.index.add(content_of(item))
        for i, fact in enumerate(case.memory):
            runtime.memory.remember_user_fact(fact, utterance_id=f"b5-{i}")
        outcome = await asyncio.wait_for(runtime.orchestrator.handle(case.user, store=runtime.store), CASE_TIMEOUT_S)
    finally:
        runtime.memory.close()
    cap = ev.capture
    if cap is None:
        raise SetupError(f"{case.id}: the graded step was never reached ({outcome.phase.value}: {outcome.code})")
    setup_refusals = _refusals(audit.records[: cap.audit_mark])
    if setup_refusals:
        raise SetupError(f"{case.id}: scripted steps were refused: {setup_refusals}")
    result = CaseResult(
        case.id,
        rep,
        case.injection,
        case.kind,
        False,
        [],
        latency_ms=_ms(cap.started, cap.ended),
        ttft_ms=_ms(cap.started, cap.first),
        prompt_tokens=cap.usage.prompt_tokens if cap.usage else None,
        completion_tokens=cap.usage.completion_tokens if cap.usage else None,
        calls=_summarize_calls(cap.calls),
        text_chars=len(cap.text),
    )
    if cap.error is not None:
        result.reasons = [f"backend error {cap.error}"]
        return result
    if outcome.code in ("DEADLINE", "TAINT_LEAK", "INTERNAL", "AUDIT_UNAVAILABLE"):
        result.reasons.append(f"request ended {outcome.phase.value}: {outcome.code}")
    if isinstance(case.expect, Calls):
        result.reasons += grade_calls(case.expect, cap.calls)
        result.reasons += [f"refused at dispatch: {r}" for r in _refusals(audit.records[cap.audit_mark :])]
    elif isinstance(case.expect, Answer):
        result.reasons += grade_answer(case.expect, cap.text, cap.calls)
        if not cap.calls and outcome.phase is not Phase.DONE:
            result.reasons.append(f"request ended {outcome.phase.value}")
    else:
        raise SetupError(f"{case.id}: a planner case needs Calls or Answer")
    result.passed = not result.reasons
    return result


async def run_reader_case(case: Case, live: LLMBackend, rep: int) -> CaseResult:
    assert case.reader_item is not None and case.reader_task is not None and isinstance(case.expect, ReaderOut)  # noqa: S101
    audit = RecordingAuditSink("atlas-core")
    ev = EvalBackend(live, grade="reader", audit=audit, max_ctx=live.capabilities().max_ctx)
    runtime = _runtime(ev, audit)
    try:
        item = content_of(case.reader_item)
        runtime.index.add(item)
        ref = runtime.store.bind_item(_ITEM_PREFIX[case.reader_item.kind], item, f"{case.reader_item.kind} item")
        reasons: list[str] = []
        try:
            await asyncio.wait_for(
                runtime.reader.read(runtime.store, ref, ReaderTask(case.reader_task), deadline=time.monotonic() + 60.0),
                CASE_TIMEOUT_S,
            )
        except ReaderRefused as exc:
            reasons.append(f"reader refused: {exc.code}")
    finally:
        runtime.memory.close()
    cap = ev.capture
    if cap is None:
        raise SetupError(f"{case.id}: the reader never called the model")
    if not reasons:
        value = strict_json_loads(cap.text.strip())  # accepted by the reader, so this parses
        reasons += check_args(value, case.expect.checks)
    return CaseResult(
        case.id,
        rep,
        case.injection,
        case.kind,
        not reasons,
        reasons,
        latency_ms=_ms(cap.started, cap.ended),
        ttft_ms=_ms(cap.started, cap.first),
        prompt_tokens=cap.usage.prompt_tokens if cap.usage else None,
        completion_tokens=cap.usage.completion_tokens if cap.usage else None,
        calls=_summarize_calls(cap.calls),
        text_chars=len(cap.text),
    )


async def run_case(case: Case, live: LLMBackend, rep: int = 0) -> CaseResult:
    if case.kind == "reader":
        return await run_reader_case(case, live, rep)
    return await run_planner_case(case, live, rep)


def _ms(start: float, end: float | None) -> int | None:
    return None if end is None else round((end - start) * 1000)


# --- the oracle: each case's intended answer, as a model ----------------------------------------


def oracle_turn(case: Case) -> Turn:
    if isinstance(case.oracle, str):
        return text_turn(case.oracle)
    if isinstance(case.oracle, tuple):
        return call_turn(*case.oracle)
    return text_turn(json.dumps(case.oracle))


def oracle_backend(case: Case) -> ScriptedBackend:
    return ScriptedBackend([oracle_turn(case)])


# --- TTFT and decode ----------------------------------------------------------------------------

_VOCAB: Final = (
    "the lighthouse keeper logged the weather each evening and noted wind speed tide height cloud cover "
    "visibility and the passing ships while the lamp turned slowly above the rocks the village below kept "
    "its own records of fishing catches market prices school terms and the long winter storms"
)
_WORDS: Final = _VOCAB.split()


def filler(words: int, seed: int) -> str:
    """Neutral, deterministic text: sentences of 12 words from a small vocabulary."""
    rng = random.Random(seed)  # noqa: S311 - filler text, not security
    sentences = []
    for _ in range(max(1, words // 12)):
        sentence = " ".join(rng.choice(_WORDS) for _ in range(12))
        sentences.append(sentence[0].upper() + sentence[1:] + ".")
    return " ".join(sentences)


def perf_messages(words: int, seed: int) -> list[Message]:
    user = (
        f"Request {secrets.token_hex(6)}. Here is a log to read:\n\n{filler(words, seed)}\n\n"
        "Without using any tools, write a summary of the log in about 150 words."
    )
    return [Message(Role.SYSTEM, SYSTEM_PROMPT.text), Message(Role.USER, user)]


@dataclass
class PerfTrial:
    prompt_tokens: int | None
    completion_tokens: int | None
    ttft_ms: int | None  # first generated token of any kind (prefill latency)
    total_ms: int
    decode_tps: float | None  # completion tokens per second after the first token
    visible_ms: int | None = None  # first token the user would see (thinking models: after reasoning)
    reasoning_tokens: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class PerfTarget:
    """LM Studio, reached directly: the LLMBackend contract drops reasoning deltas, which TTFT must count."""

    endpoint: Endpoint
    headers: Mapping[str, str]
    model: str
    reasoning_effort: str | None = None

    @classmethod
    def of(cls, base_url: str, token_file: Path | None, model: str, reasoning_effort: str | None = None) -> PerfTarget:
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if token_file is not None:
            headers["Authorization"] = f"Bearer {read_token_file(token_file)}"
        return cls(parse_http_url(base_url), headers, model, reasoning_effort)

    def __repr__(self) -> str:  # never show headers
        return f"PerfTarget({self.model})"


async def perf_trial(
    target: PerfTarget, words: int, seed: int, clock: Callable[[], float] = time.monotonic
) -> PerfTrial:
    payload = {
        "model": target.model,
        "messages": [{"role": m.role.value, "content": m.content} for m in perf_messages(words, seed)],
        "max_tokens": 320,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if target.reasoning_effort is not None:
        payload["reasoning_effort"] = target.reasoning_effort
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    started = clock()
    first: float | None = None
    visible: float | None = None
    usage: Mapping[str, Any] = {}
    pending = b""
    try:
        async with request(
            target.endpoint, "POST", "/v1/chat/completions", headers=dict(target.headers), body=body,
            deadline=time.monotonic() + 120.0,
        ) as resp:  # fmt: skip
            if resp.status != 200:
                return PerfTrial(None, None, None, round((clock() - started) * 1000), None, error=f"HTTP {resp.status}")
            async for chunk in resp.chunks():
                pending += chunk
                while (nl := pending.find(b"\n")) >= 0:
                    line, pending = pending[:nl].strip(), pending[nl + 1 :]
                    if not line.startswith(b"data:") or line[5:].strip() == b"[DONE]":
                        continue
                    obj = json.loads(line[5:])
                    if isinstance(obj.get("usage"), dict):
                        usage = obj["usage"]
                    for choice in (obj.get("choices") or [])[:1]:
                        delta = choice.get("delta") or {}
                        if first is None and (delta.get("content") or delta.get("reasoning_content")):
                            first = clock()
                        if visible is None and delta.get("content"):
                            visible = clock()
    except (HttpTransportError, HttpDeadlineError, ValueError) as exc:
        return PerfTrial(None, None, None, round((clock() - started) * 1000), None, error=type(exc).__name__)
    ended = clock()
    completion = usage.get("completion_tokens")
    details = usage.get("completion_tokens_details")
    reasoning = details.get("reasoning_tokens") if isinstance(details, dict) else None
    tps = None
    if isinstance(completion, int) and first is not None and completion >= 16 and ended > first:
        tps = round((completion - 1) / (ended - first), 1)
    prompt = usage.get("prompt_tokens")
    return PerfTrial(
        prompt if isinstance(prompt, int) else None,
        completion if isinstance(completion, int) else None,
        _ms(started, first),
        round((ended - started) * 1000),
        tps,
        visible_ms=_ms(started, visible),
        reasoning_tokens=reasoning if isinstance(reasoning, int) else None,
    )


async def perf_trials(target: PerfTarget, n: int, out: Any) -> list[PerfTrial]:
    """One calibration request (also a warm-up, not counted), then ``n`` measured ones."""
    words = 1400
    calib = await perf_trial(target, words, 0)
    if calib.prompt_tokens:
        words = max(100, round(words * PERF_PROMPT_TOKENS / calib.prompt_tokens))
    print(f"perf: calibration {calib.prompt_tokens} prompt tokens -> {words} words", file=out)
    trials = []
    for i in range(n):
        trial = await perf_trial(target, words, i + 1)
        print(
            f"perf {i + 1}/{n}: ttft {trial.ttft_ms} ms, first visible {trial.visible_ms} ms, "
            f"{trial.decode_tps} tok/s, {trial.reasoning_tokens} reasoning tokens",
            file=out,
        )
        trials.append(trial)
    return trials


# --- statistics and the report ------------------------------------------------------------------


def percentile(values: Sequence[float], p: float) -> float | None:
    """Nearest-rank percentile; None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[rank - 1]


def atlas_revision() -> str:
    """The commit the run used (the prompts and tool descriptions are ATLAS code), plus "-dirty" if changed."""
    try:
        sha = subprocess.run(  # noqa: S603 - fixed arguments
            ["git", "-C", str(HERE), "rev-parse", "--short=12", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout.strip()
        dirty = subprocess.run(  # noqa: S603
            ["git", "-C", str(HERE.parents[1]), "status", "--porcelain", "--", "backend/atlas", "bench/B5-llm-matrix"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return sha + ("-dirty" if dirty else "")


def cases_digest() -> str:
    """Identifies the eval set, so runs on different case versions aren't compared by mistake."""
    src = inspect.getsource(case_set)
    return hashlib.sha256(src.encode("utf-8")).hexdigest()[:16]


def summarize(run: Mapping[str, Any]) -> dict[str, Any]:
    results = run["cases"]
    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    inj = [r for r in results if r["injection"]]
    normal = [r for r in results if not r["injection"]]
    perf = [p for p in run.get("perf", []) if p.get("error") is None]
    ttft = [p["ttft_ms"] for p in perf if p.get("ttft_ms") is not None]
    tps = [p["decode_tps"] for p in perf if p.get("decode_tps") is not None]
    visible = [p["visible_ms"] for p in perf if p.get("visible_ms") is not None]
    reasoning = [p["reasoning_tokens"] for p in perf if p.get("reasoning_tokens")]
    ptok = [p["prompt_tokens"] for p in perf if p.get("prompt_tokens")]
    latency = [r["latency_ms"] for r in results if r.get("latency_ms") is not None and r["kind"] == "planner"]
    accuracy = passed / total if total else None
    ttft_p50 = percentile(ttft, 50)
    tps_p50 = percentile(tps, 50)
    vram = run.get("vram_mb")
    checks = {
        "accuracy": None if accuracy is None else accuracy >= MIN_ACCURACY,
        "ttft": None if ttft_p50 is None else ttft_p50 <= MAX_TTFT_MS,
        "decode": None if tps_p50 is None else tps_p50 >= MIN_DECODE_TPS,
        "vram": None if vram is None else vram <= MAX_VRAM_MB,
    }
    return {
        "total": total,
        "passed": passed,
        "accuracy": accuracy,
        "normal": (sum(r["passed"] for r in normal), len(normal)),
        "injection": (sum(r["passed"] for r in inj), len(inj)),
        "ttft_p50": ttft_p50,
        "ttft_p95": percentile(ttft, 95),
        "tps_p50": tps_p50,
        "visible_p50": percentile(visible, 50),
        "reasoning": round(sum(reasoning) / len(reasoning)) if reasoning else 0,
        "prompt_tokens": round(sum(ptok) / len(ptok)) if ptok else None,
        "step_p50": percentile(latency, 50),
        "step_p95": percentile(latency, 95),
        "vram": vram,
        "checks": checks,
        "meets": all(v is True for v in checks.values()),
    }


def _fmt(value: Any, unit: str = "") -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        value = f"{value:.1f}" if value % 1 else f"{value:.0f}"
    return f"{value}{unit}"


def _mark(ok: bool | None) -> str:
    return {True: "pass", False: "FAIL", None: "not measured"}[ok]


def report(runs: Sequence[Mapping[str, Any]]) -> str:
    lines = [
        "| Model | Quant | Ctx | Tool-call accuracy | Normal | Injection | TTFT p50 / p95 (prompt) | "
        "First visible p50 (reasoning tok) | Decode p50 | Planner step p50 / p95 | VRAM | Verdict |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for run in runs:
        s = summarize(run)
        m = run.get("model", {})
        misses = [k for k, v in s["checks"].items() if v is not True]
        verdict = "meets B5" if s["meets"] else "misses: " + ", ".join(f"{k} ({_mark(s['checks'][k])})" for k in misses)
        effort = f" (reasoning_effort {m['reasoning_effort']})" if m.get("reasoning_effort") else ""
        acc = "n/a" if s["accuracy"] is None else f"{s['passed']}/{s['total']} ({s['accuracy']:.0%})"
        lines.append(
            f"| `{m.get('id', '?')}`{effort} | {m.get('quantization') or 'n/a'} | {_fmt(m.get('context'))} | {acc} | "
            f"{s['normal'][0]}/{s['normal'][1]} | {s['injection'][0]}/{s['injection'][1]} | "
            f"{_fmt(s['ttft_p50'], ' ms')} / {_fmt(s['ttft_p95'], ' ms')} ({_fmt(s['prompt_tokens'])} tok) | "
            f"{_fmt(s['visible_p50'], ' ms')} ({s['reasoning']}) | {_fmt(s['tps_p50'], ' tok/s')} | {_fmt(s['step_p50'], ' ms')} / {_fmt(s['step_p95'], ' ms')} | "
            f"{_fmt(s['vram'], ' MB')} | {verdict} |"
        )
    lines += [
        "",
        f"Criteria (PLAN.md §2.8): accuracy ≥ {MIN_ACCURACY:.0%} over every graded attempt; TTFT p50 ≤ "
        f"{MAX_TTFT_MS:.0f} ms at a ~{PERF_PROMPT_TOKENS}-token prompt (first token of any kind, reasoning included; "
        "the first visible token is reported beside it); decode p50 ≥ {MIN_DECODE_TPS:.0f} tok/s; "
        f"VRAM ≤ {MAX_VRAM_MB} MB in total (read from nvidia-smi on the desktop).",
    ]
    failures: dict[str, dict[str, list[str]]] = {}
    for run in runs:
        model = run.get("model", {}).get("id", "?")
        for r in run["cases"]:
            if not r["passed"]:
                failures.setdefault(r["case"], {}).setdefault(model, []).extend(r["reasons"][:2])
    if failures:
        titles = {c.id: c.title for c in CASES}
        lines += ["", "Failed cases:", "", "| Case | Model | Why (first reasons) |", "|---|---|---|"]
        for cid in sorted(failures):
            for model, reasons in failures[cid].items():
                why = "; ".join(dict.fromkeys(reasons))[:300].replace("|", "\\|")
                lines.append(f"| {cid} {titles.get(cid, '')} | `{model}` | {why} |")
    notes = [(run.get("model", {}).get("id", "?"), run.get("note")) for run in runs if run.get("note")]
    if notes:
        lines += ["", "Notes:", ""] + [f"- `{model}`: {note}" for model, note in notes]
    digests = sorted({run.get("harness", {}).get("cases_digest", "?") for run in runs})
    if len(digests) > 1:
        lines += ["", f"WARNING: these runs used different case sets ({', '.join(digests)}); don't compare them."]
    return "\n".join(lines) + "\n"


# --- the CLI ------------------------------------------------------------------------------------


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")[:60] or "model"


async def _model_info(live: LLMBackend, wanted: str | None) -> dict[str, Any]:
    deadline = time.monotonic() + 10.0
    models = await live.list_models(deadline)
    loaded = [m for m in models if m.loaded and m.extra.get("type", "llm") in ("llm", "vlm")]
    pick = next((m for m in loaded if m.id == wanted), None) if wanted else (loaded[0] if len(loaded) == 1 else None)
    if pick is None:
        names = ", ".join(m.id for m in loaded) or "none"
        raise SystemExit(f"load exactly the model to test in LM Studio first (loaded: {names})")
    return {"id": pick.id, "quantization": pick.quantization, "context": pick.max_ctx, "arch": pick.extra.get("arch")}


async def run_all(args: argparse.Namespace, out: Any) -> dict[str, Any]:
    selected = [c for c in CASES if not args.cases or c.id in args.cases]
    if args.cases and len(selected) != len(set(args.cases)):
        raise SystemExit("unknown case id in --cases")
    run: dict[str, Any] = {
        "bench": "B5",
        "schema": RESULT_SCHEMA,
        "started": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "harness": {
            "cases_digest": cases_digest(),
            "atlas": atlas_revision(),
            "repeat": 0 if args.no_eval else args.repeat,
            "cases": 0 if args.no_eval else len(selected),
        },
        "vram_mb": args.vram_mb,
        "note": args.note,
    }
    if args.backend == "oracle":
        run["model"] = {"id": "oracle", "quantization": None, "context": None}
        results = []
        for case in selected:
            for rep in range(args.repeat):
                results.append(await run_case(case, oracle_backend(case), rep))
        run["cases"] = [asdict(r) for r in results]
        run["perf"] = []
        return run

    from atlas.llm.lmstudio import LMStudioBackend

    probe = LMStudioBackend.from_token_file(args.base_url, args.token_file, model=args.model)
    run["model"] = await _model_info(probe, args.model)
    # The reader sizes item text from the context window, so use the one the model was loaded with.
    max_ctx = args.max_ctx or run["model"]["context"] or 16_384
    run["model"]["context"] = max_ctx
    run["model"]["reasoning_effort"] = args.reasoning_effort
    live = LMStudioBackend.from_token_file(
        args.base_url,
        args.token_file,
        model=run["model"]["id"],
        max_ctx=max_ctx,
        reasoning_effort=args.reasoning_effort,
    )
    ready = await live.ensure_ready(run["model"]["id"], time.monotonic() + 120.0)
    if not isinstance(ready, Ready):
        raise SystemExit(f"model not ready: {ready.stage.value} ({ready.reason})")
    print(f"model {run['model']['id']} ready (warm-up {ready.warmup_ms} ms)", file=out)
    target = PerfTarget.of(args.base_url, args.token_file, run["model"]["id"], args.reasoning_effort)
    run["perf"] = [asdict(t) for t in await perf_trials(target, args.perf_trials, out)] if args.perf_trials else []
    results = []
    for rep in range(0 if args.no_eval else args.repeat):
        for case in selected:
            result = await run_case(case, live, rep)
            mark = "ok  " if result.passed else "FAIL"
            why = "" if result.passed else " - " + "; ".join(result.reasons)[:160]
            print(f"{mark} {case.id} r{rep} {result.latency_ms} ms{why}", file=out)
            results.append(result)
    run["cases"] = [asdict(r) for r in results]
    return run


def _write(run: Mapping[str, Any], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = run["started"].replace(":", "").replace("-", "")
    path = out_dir / f"B5-{stamp}-{_slug(run['model']['id'])}.json"
    path.write_text(json.dumps(run, indent=1) + "\n", encoding="utf-8")
    return path


def main(argv: Sequence[str] | None = None, out: Any = sys.stdout) -> int:
    parser = argparse.ArgumentParser(prog="b5.py", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("cases", help="list the eval cases")
    p_run = sub.add_parser("run", help="run the eval and the TTFT/decode trials")
    p_run.add_argument("--backend", choices=("lmstudio", "oracle"), default="lmstudio")
    p_run.add_argument("--base-url", default="http://10.77.0.2:1234")
    p_run.add_argument("--token-file", type=Path, help="file holding the LM Studio API token (never the token itself)")
    p_run.add_argument("--model", help="model id; default: the one model loaded in LM Studio")
    p_run.add_argument(
        "--reasoning-effort",
        choices=("none", "low", "medium", "high"),
        help="sent with every request (default: not sent)",
    )
    p_run.add_argument("--max-ctx", type=int, help="context length; default: what LM Studio reports as loaded")
    p_run.add_argument("--repeat", type=int, default=3, help="passes over the case set (default 3)")
    p_run.add_argument("--perf-trials", type=int, default=5)
    p_run.add_argument("--no-eval", action="store_true", help="only the TTFT/decode trials")
    p_run.add_argument("--cases", type=lambda s: [x.strip() for x in s.split(",") if x.strip()], default=[])
    p_run.add_argument("--vram-mb", type=int, help="total GPU memory used with the model loaded (nvidia-smi)")
    p_run.add_argument("--note", help="free text for the report (LM Studio version, settings)")
    p_run.add_argument("--out", type=Path, default=RAW_DIR)
    p_rep = sub.add_parser("report", help="markdown table from run files")
    p_rep.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args(argv)

    if args.cmd == "cases":
        for c in CASES:
            tag = "INJ" if c.injection else "   "
            what = c.user or f"reader {c.reader_task}: {c.reader_item.resource_id if c.reader_item else ''}"
            print(f"{c.id} {tag} {c.kind:7} {c.title:40} {what[:70]}", file=out)
        print(f"{len(CASES)} cases, {sum(c.injection for c in CASES)} injection; digest {cases_digest()}", file=out)
        return 0
    if args.cmd == "report":
        runs = [json.loads(f.read_text(encoding="utf-8")) for f in args.files]
        for f, r in zip(args.files, runs, strict=True):
            if r.get("bench") != "B5" or r.get("schema") != RESULT_SCHEMA:
                raise SystemExit(f"{f.name}: not a B5 result file (schema {RESULT_SCHEMA})")
        print(report(runs), end="", file=out)
        return 0
    if args.repeat < 1 or args.perf_trials < 0:
        raise SystemExit("--repeat must be at least 1 and --perf-trials at least 0")
    if args.token_file is not None:
        args.token_file = args.token_file.expanduser()
    run = asyncio.run(run_all(args, out))
    path = _write(run, args.out)
    s = summarize(run)
    print(f"\n{s['passed']}/{s['total']} passed; wrote {path}", file=out)
    print(report([run]), end="", file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
