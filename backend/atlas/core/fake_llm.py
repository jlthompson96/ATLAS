"""Scripted fake ``LLMBackend`` for tests and the CLI on a machine without LM Studio.

``ScriptedBackend`` answers each ``chat`` call from a script (a list of turns) or a responder
function, and records every call so tests can assert on exactly what each context was shown.
``demo_responder`` is the CLI default: a tiny rule-based "model" good enough to exercise the
orchestrator end to end (time, memory search, inbox listing, reader output that fits the schema).

The adversarial model used by the red-team tests lives in ``tests/core_support.py``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from atlas.llm.interface import (
    Capabilities,
    Delta,
    FinishReason,
    Health,
    LLMBackend,
    Message,
    ModelInfo,
    NotReady,
    Ready,
    Role,
    ToolCall,
    ToolSpec,
    Usage,
)

Turn = Sequence[Delta]
Responder = Callable[[Sequence[Message], Sequence[ToolSpec] | None, Mapping[str, Any] | None], Turn]


@dataclass(frozen=True, slots=True)
class RecordedCall:
    messages: tuple[Message, ...]
    tools: tuple[ToolSpec, ...] | None
    response_schema: Mapping[str, Any] | None

    @property
    def is_reader(self) -> bool:
        return self.response_schema is not None

    def text(self) -> str:
        return "\n".join(m.content for m in self.messages) + "".join(
            c.name + c.arguments_json for m in self.messages for c in m.tool_calls
        )


def text_turn(text: str) -> Turn:
    return [Delta(text=text, finish_reason=FinishReason.STOP, usage=Usage(len(text) // 4 + 1, len(text) // 4 + 1))]


def call_turn(*calls: tuple[str, Mapping[str, Any] | str]) -> Turn:
    out = []
    for i, (name, args) in enumerate(calls):
        out.append(ToolCall(f"c{i}", name, args if isinstance(args, str) else json.dumps(args)))
    return [Delta(tool_calls=tuple(out), finish_reason=FinishReason.TOOL_CALLS, usage=Usage(10, 10))]


def example_for(schema: Mapping[str, Any]) -> Any:
    """The simplest value matching one of our own (atlas.core.schema) JSON Schemas."""
    if "anyOf" in schema:
        return example_for(schema["anyOf"][-1])
    kind = schema.get("type")
    if "enum" in schema:
        return schema["enum"][0]
    if kind == "object":
        return {k: example_for(v) for k, v in schema.get("properties", {}).items() if k in schema.get("required", [])}
    if kind == "array":
        return []
    if kind == "integer":
        return schema.get("minimum", 0)
    if kind == "boolean":
        return False
    return "scripted text"[: schema.get("maxLength", 13)]


def demo_responder(
    messages: Sequence[Message], tools: Sequence[ToolSpec] | None, schema: Mapping[str, Any] | None
) -> Turn:
    if schema is not None:
        return text_turn(json.dumps(example_for(schema)))
    last = messages[-1]
    if last.role is Role.TOOL:
        return text_turn("Here is what I found:\n" + last.content)
    text = last.content.lower()
    if "time" in text or "date" in text:
        return call_turn(("get_time", {}))
    if any(w in text for w in ("remember", "recall", "memory", "know about")):
        return call_turn(("search_memory", {"query": last.content.replace("$", " ")[:200].strip() or "memory"}))
    if any(w in text for w in ("email", "inbox", "mail", "messages")):
        return call_turn(("read_index", {"source": "all", "limit": 5}))
    return text_turn("(scripted model) I can tell the time, search memory and list your inbox.")


class ScriptedBackend(LLMBackend):
    def __init__(
        self, script: Sequence[Turn] | None = None, *, responder: Responder | None = None, max_ctx: int = 16_384
    ) -> None:
        if script is None and responder is None:
            responder = demo_responder
        self._script = list(script or [])
        self._responder = responder
        self._max_ctx = max_ctx
        self.calls: list[RecordedCall] = []

    def id(self) -> str:
        return "scripted@local"

    def capabilities(self) -> Capabilities:
        return Capabilities(tools=True, json_schema=True, streaming=True, max_ctx=self._max_ctx)

    async def health(self, deadline: float) -> Health:
        return Health(True, True, "scripted")

    async def ensure_ready(self, model: str, deadline: float) -> Ready | NotReady:
        return Ready(model, 0)

    async def list_models(self, deadline: float) -> Sequence[ModelInfo]:
        return [ModelInfo("scripted", True, self._max_ctx)]

    def _next(
        self, messages: Sequence[Message], tools: Sequence[ToolSpec] | None, schema: Mapping[str, Any] | None
    ) -> Turn:
        if self._script:
            return self._script.pop(0)
        if self._responder is not None:
            return self._responder(messages, tools, schema)
        return text_turn("(script exhausted)")

    async def chat(
        self,
        messages: Sequence[Message],
        *,
        max_tokens: int,
        tools: Sequence[ToolSpec] | None = None,
        response_schema: Mapping[str, Any] | None = None,
        deadline: float | None = None,
    ) -> AsyncIterator[Delta]:
        self.calls.append(RecordedCall(tuple(messages), None if tools is None else tuple(tools), response_schema))
        for delta in self._next(messages, tools, response_schema):
            yield delta

    def planner_calls(self) -> list[RecordedCall]:
        return [c for c in self.calls if not c.is_reader]

    def reader_calls(self) -> list[RecordedCall]:
        return [c for c in self.calls if c.is_reader]
