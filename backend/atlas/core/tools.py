"""Planner tools (PLAN.md §2.9): ``read_index``, ``search_memory``, ``queue_research``,
``propose_action``, ``ask_reader``, plus the Phase 1 read-only ``get_time``.

None of these acts on the outside world. Each tool has a fixed tag (``atlas.policy.types.ToolTag``)
that the registry and the policy engine key off, a closed argument schema, and returns a
``ToolOutput`` made only of ``TrustedText`` and ``VarRef`` parts, so its result can go back into the
planner prompt. Data derived from untrusted items is bound as ``$VAR`` references; what the planner
saw that was *influenced* by untrusted data (enums, metadata) is reported as ``influence`` so later
proposals carry the provenance flag.

Write tools do not exist here. They live only in ``atlas.executor.senders`` (UW-1).
"""

from __future__ import annotations

import hashlib
import secrets
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any, ClassVar, Final

from atlas.core.auditing import AuditSink, DataRef, tool_call_record
from atlas.core.index import ItemIndex
from atlas.core.pending import PROPOSE_ARGS, ActionProposer, Draft, DraftError, EventSink, draft_payload
from atlas.core.prompt import AtlasText, PromptPart, TrustedText, gen, t
from atlas.core.reader import QuarantinedReader, ReaderRefused, ReaderTask
from atlas.core.schema import Bool, Enum, Int, Obj, PlainText, Ref, collect_refs
from atlas.core.taint import (
    EMPTY_PROVENANCE,
    Provenance,
    SourceKind,
    UndefinedVariable,
    UntrustedContent,
    VariableStore,
    VarRef,
    size_bucket,
)
from atlas.llm.interface import ToolSpec
from atlas.memory.store import MemoryStore
from atlas.policy.engine import Allowlists, ArgTaint, Decision, ProposedAction
from atlas.policy.types import Context, ToolTag


class ToolError(Exception):
    """A tool call that can't proceed. ``code`` is UPPER_SNAKE and safe to show the planner."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class RequestCtx:
    """Per-request state shared by the orchestrator and the tools."""

    store: VariableStore
    context: Context
    correlation_id: str
    deadline: float  # absolute time.monotonic()
    audit: AuditSink
    influence: Provenance = EMPTY_PROVENANCE
    calls: Counter[str] = field(default_factory=Counter)
    proposals: int = 0
    tokens: int = 0
    steps: int = 0
    pending_actions: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Prepared:
    args: ArgTaint
    action: ProposedAction | None = None
    draft: Draft | None = None


@dataclass(frozen=True, slots=True)
class ToolOutput:
    lines: tuple[tuple[PromptPart, ...], ...]
    data_accessed: tuple[DataRef, ...] = ()
    influence: Provenance = EMPTY_PROVENANCE
    status: str = "ok"  # audit result status
    code: str | None = None

    def parts(self) -> list[PromptPart]:
        out: list[PromptPart] = []
        for i, line in enumerate(self.lines):
            if i:
                out.append(t("\n"))
            out.extend(line)
        return out


def text_output(text: AtlasText, *, status: str = "ok", code: str | None = None) -> ToolOutput:
    if not isinstance(text, AtlasText):
        raise TypeError("text_output takes ATLAS-generated text (t() or gen())")
    return ToolOutput(((text,),), status=status, code=code)


def arg_taint(args: Mapping[str, Any], store: VariableStore) -> ArgTaint:
    refs = collect_refs(dict(args))
    provenance = EMPTY_PROVENANCE
    items = False
    for name in refs:
        try:
            var = store.get(name)
        except UndefinedVariable:
            raise ToolError("UNDEFINED_VARIABLE") from None
        provenance = provenance.union(var.provenance)
        items |= var.is_item
    return ArgTaint(has_refs=bool(refs), refs_to_items=items, provenance=provenance)


class Tool(ABC):
    name: ClassVar[str]
    tag: ClassVar[ToolTag]
    description: ClassVar[str]
    args: ClassVar[Obj]

    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.description, self.args.json_schema())

    def prepare(self, args: Mapping[str, Any], rc: RequestCtx) -> Prepared:
        return Prepared(arg_taint(args, rc.store))

    @abstractmethod
    async def run(
        self, args: Mapping[str, Any], prepared: Prepared, decision: Decision, rc: RequestCtx
    ) -> ToolOutput: ...


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%dT%H:%MZ")


# --- read tools ------------------------------------------------------------------------------------


def _utc_offset(offset: timedelta | None) -> str:
    minutes = int((offset or timedelta()).total_seconds()) // 60
    sign = "-" if minutes < 0 else "+"
    return f"UTC{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


class GetTime(Tool):
    """The time as a person reads it, in the Pi's own time zone, plus UTC for precision.

    A model shown only UTC would tell the owner the wrong hour: it doesn't know where the Pi is.
    """

    name = "get_time"
    tag = ToolTag.READ
    description = "Current local date and time, with the time zone and UTC offset, and the same moment in UTC."
    args = Obj({})

    def __init__(self, clock_ms: Callable[[], int], tz: tzinfo | None = None) -> None:
        self._clock = clock_ms
        self._tz = tz  # None: the system's zone (/etc/localtime on the Pi)

    async def run(self, args: Mapping[str, Any], prepared: Prepared, decision: Decision, rc: RequestCtx) -> ToolOutput:
        utc = datetime.fromtimestamp(self._clock() / 1000, UTC)
        local = utc.astimezone(self._tz)
        clock = f"{local.hour % 12 or 12}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"
        day = f"{local:%A}, {local:%B} {local.day}, {local.year}"
        return text_output(
            gen(
                "now: {clock}, {day} ({zone}, {offset}); UTC {utc}",
                clock=clock,
                day=day,
                zone=local.tzname() or "local",
                offset=_utc_offset(local.utcoffset()),
                utc=utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
        )


_SOURCE_PREFIX: Final = {
    SourceKind.EMAIL: "EMAIL",
    SourceKind.DISCORD: "DISCORD",
    SourceKind.CALENDAR: "EVENT",
    SourceKind.WEB: "PAGE",
}


class ReadIndex(Tool):
    name = "read_index"
    tag = ToolTag.READ
    description = (
        "List recent email, Discord or calendar items as metadata only. Each item comes back as a $VAR "
        "reference; sender and subject are $VAR references too. Use ask_reader to learn about an item."
    )
    args = Obj(
        {
            "source": Enum(("email", "discord", "calendar", "all")),
            "limit": Int(1, 20),
            "unread_only": Bool(),
        },
        required=frozenset({"source"}),
    )

    def __init__(self, index: ItemIndex) -> None:
        self._index = index

    async def run(self, args: Mapping[str, Any], prepared: Prepared, decision: Decision, rc: RequestCtx) -> ToolOutput:
        source = None if args["source"] == "all" else SourceKind(args["source"])
        items = self._index.list(source, limit=args.get("limit", 10), unread_only=args.get("unread_only", False))
        lines: list[tuple[PromptPart, ...]] = []
        accessed: list[DataRef] = []
        influence = EMPTY_PROVENANCE
        for item in items:
            prefix = _SOURCE_PREFIX.get(item.source.kind, "ITEM")
            ref = rc.store.bind_item(prefix, item, f"{item.source.kind.value} item")
            parts: list[PromptPart] = [
                ref,
                gen(
                    " {rid}, received {at}, {size}{unread}; from ",
                    rid=item.source.resource_id,
                    at=_iso(item.source.observed_at_ms),
                    size=size_bucket(item.byte_len),
                    unread=", unread" if item.unread else "",
                ),
            ]
            contact = self._index.contact_name(item)
            if contact is not None:
                try:
                    parts.append(gen("contact '{name}' (claimed)", name=contact))
                except TypeError:
                    parts.append(t("a configured contact (claimed)"))
            elif item.sender is not None:
                parts.append(rc.store.bind_text("FROM", item.sender, "sender"))
            else:
                parts.append(t("(unknown)"))
            if item.subject is not None:
                parts += [t("; subject "), rc.store.bind_text("SUBJECT", item.subject, "subject")]
            lines.append(tuple(parts))
            accessed.append(DataRef(item.source.resource_id, item.byte_len, item.sha256(), True))
            influence = influence.union(item.provenance)
        if not lines:
            lines.append((t("no items"),))
        return ToolOutput(tuple(lines), tuple(accessed), influence)


class SearchMemory(Tool):
    name = "search_memory"
    tag = ToolTag.READ
    description = (
        "Keyword search of ATLAS's memory. Things the user said come back as text; anything learned from "
        "email, Discord, calendar or the web comes back as a tainted $MEM reference."
    )
    args = Obj(
        {"query": PlainText(200, min_len=1, single_line=True), "limit": Int(1, 10)}, required=frozenset({"query"})
    )

    def __init__(self, memory: MemoryStore) -> None:
        self._memory = memory

    async def run(self, args: Mapping[str, Any], prepared: Prepared, decision: Decision, rc: RequestCtx) -> ToolOutput:
        hits = self._memory.search(args["query"], args.get("limit", 5))
        lines: list[tuple[PromptPart, ...]] = []
        accessed: list[DataRef] = []
        influence = EMPTY_PROVENANCE
        for hit in hits:
            if hit.tainted_value is not None:
                ref = rc.store.bind_text("MEM", hit.tainted_value, "memory from an untrusted source")
                lines.append((gen("{rid} (learned from untrusted content): ", rid=hit.resource_id), ref))
                accessed.append(DataRef(hit.resource_id, hit.tainted_value.byte_len, hit.tainted_value.sha256(), True))
                influence = influence.union(hit.tainted_value.provenance)
            elif hit.text is not None:
                # The row text is the user's words, not ATLAS's: TrustedText, so it is leak-scanned (F2).
                lines.append((gen("{rid} (the user said): ", rid=hit.resource_id), TrustedText(hit.text)))
                raw = hit.text.encode("utf-8")
                accessed.append(DataRef(hit.resource_id, len(raw), hashlib.sha256(raw).hexdigest(), False))
        if not lines:
            lines.append((t("no matching memories"),))
        return ToolOutput(tuple(lines), tuple(accessed), influence)


class AskReader(Tool):
    name = "ask_reader"
    tag = ToolTag.READER
    description = (
        "Have the quarantined reader process one item ($EMAIL_n, $DISCORD_n, $EVENT_n) with a fixed task. "
        "Returns classifications, and free text only as tainted $VAR references."
    )
    args = Obj(
        {"item": Ref(), "task": Enum(tuple(t_.value for t_ in ReaderTask))}, required=frozenset({"item", "task"})
    )

    def __init__(self, reader: QuarantinedReader) -> None:
        self._reader = reader

    async def run(self, args: Mapping[str, Any], prepared: Prepared, decision: Decision, rc: RequestCtx) -> ToolOutput:
        ref = VarRef(args["item"])
        var = rc.store.get(ref)
        if not isinstance(var.value, UntrustedContent):
            return text_output(t("refused: NOT_AN_ITEM"), status="refused", code="NOT_AN_ITEM")
        item = var.value
        task = ReaderTask(args["task"])
        accessed = (DataRef(item.source.resource_id, item.byte_len, item.sha256(), True),)
        rc.steps += 1
        try:
            result = await self._reader.read(rc.store, ref, task, deadline=rc.deadline)
        except ReaderRefused as exc:
            await rc.audit.append(
                tool_call_record(
                    context=Context.QUARANTINED, correlation_id=rc.correlation_id, tool=f"reader.{task.value}",
                    args={"item": ref.name}, status="refused", code=exc.code, data_accessed=accessed,
                )
            )  # fmt: skip
            return ToolOutput(
                ((gen("reader refused this item: {code}", code=exc.code),),),
                accessed,
                item.provenance,
                "refused",
                exc.code,
            )
        rc.tokens += result.prompt_tokens + result.completion_tokens
        await rc.audit.append(
            tool_call_record(
                context=Context.QUARANTINED, correlation_id=rc.correlation_id, tool=f"reader.{task.value}",
                args={"item": ref.name}, status="ok", data_accessed=accessed,
            )
        )  # fmt: skip
        lines = ((ref, gen(" {task}:", task=task.value)), *result.lines)
        return ToolOutput(lines, accessed, item.provenance)


# --- queue -----------------------------------------------------------------------------------------


@dataclass
class ResearchTask:
    task_id: str
    question: str
    unattended: bool


class ResearchQueue:
    """Phase 1 stand-in for the research task queue (Phase 5). Emits events.v1 ``task.progress``."""

    BUDGET_INTERACTIVE: Final = {"steps": 25, "time_s": 600, "tokens": 200_000, "bytes": 20_000_000}

    def __init__(self, events: EventSink) -> None:
        self._events = events
        self.tasks: list[ResearchTask] = []

    async def enqueue(self, question: str, *, unattended: bool) -> ResearchTask:
        task = ResearchTask("task-" + secrets.token_hex(6), question, unattended)
        self.tasks.append(task)
        await self._events.emit(
            "task.progress",
            {
                "task_id": task.task_id,
                "kind": "research",
                "status": "queued",
                "title": question[:120],
                "redacted": False,
                "step": 0,
                "budget": dict(self.BUDGET_INTERACTIVE),
                "used": {"steps": 0, "time_s": 0, "tokens": 0, "bytes": 0},
                "unattended": unattended,
            },
        )
        return task


class QueueResearch(Tool):
    name = "queue_research"
    tag = ToolTag.QUEUE
    description = (
        "Queue a one-off web research task. The question must be your own words: research never sees "
        "email, Discord, calendar or memory data, and $VAR references are refused."
    )
    args = Obj({"question": PlainText(300, min_len=1, single_line=True)}, required=frozenset({"question"}))

    def __init__(self, queue: ResearchQueue) -> None:
        self._queue = queue

    async def run(self, args: Mapping[str, Any], prepared: Prepared, decision: Decision, rc: RequestCtx) -> ToolOutput:
        task = await self._queue.enqueue(args["question"], unattended=rc.context is Context.UNATTENDED)
        return text_output(gen("queued research task {task_id}", task_id=task.task_id))


# --- propose ---------------------------------------------------------------------------------------


class ProposeAction(Tool):
    name = "propose_action"
    tag = ToolTag.PROPOSE
    description = (
        "Propose an email, Discord post or research schedule. Nothing is sent: the user reviews the exact "
        "payload in the HUD and approves it with a PIN. Give kind, summary and exactly the fields of that kind:\n"
        "- email.send: to, subject, body; optional cc, in_reply_to (the $EMAIL_n being answered).\n"
        "- discord.post: channel_id, content.\n"
        "- research.schedule: question, every_minutes (60 to 10080).\n"
        "Text fields take literal text or a $VAR name. body and content are lists of parts joined in order. "
        'A $VAR must be a whole value or a whole list entry, never inside other text: ["Summary: ", "$SUMMARY_1"].'
    )
    args = PROPOSE_ARGS

    def __init__(self, proposer: ActionProposer, channel_labels: Mapping[str, str]) -> None:
        self._proposer = proposer
        self._labels = dict(channel_labels)

    def prepare(self, args: Mapping[str, Any], rc: RequestCtx) -> Prepared:
        arg_taint(args, rc.store)  # undefined references fail first, with the same code as elsewhere
        try:
            draft = draft_payload(args, rc.store, self._labels)
        except DraftError as exc:
            raise ToolError(exc.code) from None
        return Prepared(draft.args, draft.action, draft)

    async def run(self, args: Mapping[str, Any], prepared: Prepared, decision: Decision, rc: RequestCtx) -> ToolOutput:
        assert prepared.draft is not None  # noqa: S101 - set by prepare()
        action = await self._proposer.propose(
            prepared.draft, decision, args["summary"], context=rc.context, correlation_id=rc.correlation_id
        )
        rc.pending_actions.append(action.action_id)
        warnings = ", ".join(w.value for w in action.warnings) or "none"
        flag = "tainted" if action.provenance.tainted else "trusted"
        return text_output(
            gen(
                "action {action_id} ({kind}) is pending the user's approval in the HUD; "
                "provenance: {flag}; warnings: {warnings}. It has not been sent.",
                action_id=action.action_id,
                kind=action.kind,
                flag=flag,
                warnings=warnings,
            )
        )


@dataclass(frozen=True)
class ToolDeps:
    clock_ms: Callable[[], int]
    index: ItemIndex
    memory: MemoryStore
    research: ResearchQueue
    proposer: ActionProposer
    reader: QuarantinedReader
    allowlists: Allowlists = field(default_factory=Allowlists)
    channel_labels: Mapping[str, str] = field(default_factory=dict)


def all_tool_types() -> Sequence[type[Tool]]:
    return (GetTime, ReadIndex, SearchMemory, AskReader, QueueResearch, ProposeAction)
