"""Wiring for the planner side of atlas-core (used by the CLI and tests).

The approval side (``atlas.core.approvals.ApprovalService``) is wired separately on purpose: the
planner runtime shares only the ``PendingActionStore`` with it, and holds no reference to approvals,
the signing key or the executor.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from atlas.core.auditing import AuditSink
from atlas.core.index import ItemIndex
from atlas.core.orchestrator import Budget, Orchestrator
from atlas.core.pending import ActionProposer, EventSink, PendingActionStore
from atlas.core.reader import QuarantinedReader
from atlas.core.taint import VariableStore
from atlas.core.tools import ResearchQueue, ToolDeps
from atlas.llm.interface import LLMBackend
from atlas.memory.store import MemoryStore
from atlas.policy.engine import Allowlists
from atlas.policy.types import Context


@dataclass
class CoreRuntime:
    store: VariableStore
    index: ItemIndex
    memory: MemoryStore
    pending: PendingActionStore
    research: ResearchQueue
    proposer: ActionProposer
    reader: QuarantinedReader
    deps: ToolDeps
    orchestrator: Orchestrator


def build_runtime(
    backend: LLMBackend,
    *,
    audit: AuditSink,
    events: EventSink,
    clock_ms: Callable[[], int],
    memory_path: Path | str = ":memory:",
    allowlists: Allowlists | None = None,
    contacts: Mapping[str, str] | None = None,
    channel_labels: Mapping[str, str] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    budgets: Mapping[Context, Budget] | None = None,
    reader_backend: LLMBackend | None = None,
) -> CoreRuntime:
    store = VariableStore(clock_ms)
    index = ItemIndex(contacts)
    memory = MemoryStore(memory_path, clock_ms)
    pending = PendingActionStore()
    research = ResearchQueue(events)
    proposer = ActionProposer(pending, audit, events, clock_ms)
    # Same model, separate context (§2.8 two-model policy): the reader gets its own backend handle.
    reader = QuarantinedReader(reader_backend or backend, monotonic=monotonic)
    deps = ToolDeps(
        clock_ms=clock_ms,
        index=index,
        memory=memory,
        research=research,
        proposer=proposer,
        reader=reader,
        allowlists=allowlists or Allowlists(),
        channel_labels=dict(channel_labels or {}),
    )
    orchestrator = Orchestrator(backend, deps, audit, budgets=budgets, monotonic=monotonic)
    return CoreRuntime(store, index, memory, pending, research, proposer, reader, deps, orchestrator)
