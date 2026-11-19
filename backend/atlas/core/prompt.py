"""The planner prompt builder. Tainted data cannot be rendered into a planner prompt (PLAN.md §2.9, rule 1).

Enforcement, in layers:

1. **Types.** ``PlannerContext`` accepts only ``TrustedText`` and ``VarRef`` parts (checked at
   runtime, not just by mypy). ``TrustedText`` accepts only an exact ``str``; ``TaintedStr`` and
   ``UntrustedContent`` refuse every conversion to ``str``. A ``VarRef`` renders as its name plus a
   trusted description (label, source IDs, size), never its content.
2. **Content scan.** ``render()`` checks every system, tool and assistant message against the
   session's ``TaintIndex`` (fingerprints of everything tainted ever bound) and raises ``TaintLeak``
   instead of returning a prompt that contains tainted text. This catches the remaining way to
   cheat, calling ``_reveal()`` and wrapping the result, which tests also forbid statically.

User messages are not scanned: they are the user's own words (trusted by definition, §2.9 diagram),
and a user quoting an email to ATLAS is their choice.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from atlas.core.taint import TaintLeak, VariableStore, VarRef
from atlas.llm.interface import Message, Role, ToolCall


@dataclass(frozen=True, slots=True)
class TrustedText:
    """Text from a trusted origin: ATLAS's own code, the user, or a closed enum. Exact ``str`` only."""

    text: str

    def __post_init__(self) -> None:
        if type(self.text) is not str:
            raise TypeError("TrustedText takes an exact str; tainted values are passed as VarRef")


PromptPart = TrustedText | VarRef


def check_parts(parts: Iterable[object]) -> tuple[PromptPart, ...]:
    out: list[PromptPart] = []
    for part in parts:
        if not isinstance(part, TrustedText | VarRef):
            raise TypeError(f"planner prompts accept TrustedText and VarRef only, not {type(part).__name__}")
        out.append(part)
    return tuple(out)


def t(text: str) -> TrustedText:
    return TrustedText(text)


@dataclass(frozen=True, slots=True)
class _Entry:
    role: Role
    parts: tuple[PromptPart, ...]
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None


class PlannerContext:
    """One planner conversation. Holds parts, renders ``Message``s for ``LLMBackend.chat``."""

    def __init__(self, system: TrustedText, store: VariableStore, trusted_corpus: Iterable[str] = ()) -> None:
        self._store = store
        self._entries: list[_Entry] = [_Entry(Role.SYSTEM, check_parts([system]))]
        store.index.add_trusted_corpus(system.text)
        for text in trusted_corpus:
            store.index.add_trusted_corpus(text)

    @property
    def store(self) -> VariableStore:
        return self._store

    def add_user(self, text: TrustedText) -> None:
        self._entries.append(_Entry(Role.USER, check_parts([text])))

    def add_assistant(self, text: TrustedText, tool_calls: Sequence[ToolCall] = ()) -> None:
        for call in tool_calls:
            if not isinstance(call, ToolCall):
                raise TypeError("tool_calls must be ToolCall values")
        self._entries.append(_Entry(Role.ASSISTANT, check_parts([text]), tuple(tool_calls)))

    def add_tool_result(self, tool_call_id: str, parts: Iterable[object]) -> None:
        self._entries.append(_Entry(Role.TOOL, check_parts(parts), tool_call_id=tool_call_id))

    def _text(self, parts: tuple[PromptPart, ...]) -> str:
        out: list[str] = []
        for part in parts:
            if isinstance(part, TrustedText):
                out.append(part.text)
            else:
                out.append(self._store.describe(part))
        return "".join(out)

    def render(self) -> list[Message]:
        """Messages for the backend. Raises ``TaintLeak`` rather than return a prompt with tainted text."""
        messages: list[Message] = []
        for entry in self._entries:
            content = self._text(entry.parts)
            scanned = [content] if entry.role is not Role.USER else []
            scanned += [call.name + call.arguments_json for call in entry.tool_calls]
            for text in scanned:
                if self._store.index.contains(text):
                    raise TaintLeak("tainted text reached a planner prompt")
            messages.append(Message(entry.role, content, entry.tool_calls, entry.tool_call_id))
        return messages
