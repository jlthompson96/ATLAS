"""Context-bound tool registries (PLAN.md §2.9 rule 3; UW-1).

One registry per execution context (audit-record.v1 ``context``):

| Context       | Tools                                                                 |
|---------------|-----------------------------------------------------------------------|
| interactive   | get_time, read_index, search_memory, ask_reader, queue_research, propose_action |
| unattended    | get_time, read_index, search_memory, ask_reader, propose_action       |
| quarantined   | none (the reader has no tools)                                         |
| system        | none                                                                   |

``ToolRegistry`` refuses, at construction, any tool whose tag is outside its context's
``CONTEXT_TAGS``. ``write`` is in no context's tags, and the only ``write``-tagged classes live in
``atlas.executor.senders``, which nothing in atlas-core imports. So a write tool is neither
importable nor constructible into a planner registry; ``tests/test_core_registry.py`` is the UW-1
static check that fails CI otherwise.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import Final

from atlas.core.tools import (
    AskReader,
    GetTime,
    ProposeAction,
    QueueResearch,
    ReadIndex,
    SearchMemory,
    Tool,
    ToolDeps,
)
from atlas.llm.interface import ToolSpec
from atlas.policy.types import CONTEXT_TAGS, Context, ToolTag


class RegistryError(TypeError):
    pass


class ToolRegistry:
    def __init__(self, context: Context, tools: Iterable[Tool]) -> None:
        allowed = CONTEXT_TAGS[context]
        self.context = context
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            tag = getattr(tool, "tag", None)
            if not isinstance(tag, ToolTag) or tag is ToolTag.WRITE or tag not in allowed:
                raise RegistryError(f"a {tag!s} tool cannot be registered in the {context.value} context")
            if not isinstance(tool, Tool):
                raise RegistryError("registries hold planner tools only")
            if tool.name in self._tools:
                raise RegistryError("duplicate tool name")
            self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def tools(self) -> Sequence[Tool]:
        return [self._tools[n] for n in self.names()]

    def specs(self) -> list[ToolSpec]:
        return [tool.spec() for tool in self.tools()]


def interactive_registry(deps: ToolDeps) -> ToolRegistry:
    return ToolRegistry(
        Context.INTERACTIVE,
        [
            GetTime(deps.clock_ms),
            ReadIndex(deps.index),
            SearchMemory(deps.memory),
            AskReader(deps.reader),
            QueueResearch(deps.research),
            ProposeAction(deps.proposer, deps.channel_labels),
        ],
    )


def unattended_registry(deps: ToolDeps) -> ToolRegistry:
    """No write tools (none exist here), and no queue: schedules change only through an approval (RT-12)."""
    return ToolRegistry(
        Context.UNATTENDED,
        [
            GetTime(deps.clock_ms),
            ReadIndex(deps.index),
            SearchMemory(deps.memory),
            AskReader(deps.reader),
            ProposeAction(deps.proposer, deps.channel_labels),
        ],
    )


def quarantined_registry(deps: ToolDeps | None = None) -> ToolRegistry:
    return ToolRegistry(Context.QUARANTINED, [])


def system_registry(deps: ToolDeps | None = None) -> ToolRegistry:
    return ToolRegistry(Context.SYSTEM, [])


REGISTRY_BUILDERS: Final[dict[Context, Callable[[ToolDeps], ToolRegistry]]] = {
    Context.INTERACTIVE: interactive_registry,
    Context.UNATTENDED: unattended_registry,
    Context.QUARANTINED: quarantined_registry,
    Context.SYSTEM: system_registry,
}
