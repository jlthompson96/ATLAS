"""The planner prompt builder. Tainted data cannot be rendered into a planner prompt (PLAN.md §2.9, rule 1).

Enforcement, in layers:

1. **Types.** ``PlannerContext`` accepts only ``TrustedText`` and ``VarRef`` parts (checked at
   runtime, not just by mypy). ``TrustedText`` accepts only an exact ``str``; ``TaintedStr`` and
   ``UntrustedContent`` refuse every conversion to ``str``. A ``VarRef`` renders as its name plus a
   description ATLAS writes (label, source IDs, size bucket), never its content.
2. **Content scan** against the session's ``TaintIndex``, aimed only at text that is not
   ATLAS-generated (review F2), so an attacker can't make ATLAS's own output look like a leak:

   * ``AtlasText`` (built only by ``t()`` from a constant, or ``gen()`` from a constant template plus
     ints, bools and short safe strings) is never scanned, and neither are ``VarRef`` descriptions;
   * other ``TrustedText`` parts of a tool result (e.g. a memory row the user stated) are scanned one
     by one when the result is added; a hit withholds that one result (``add_tool_result`` returns
     False) instead of failing the request;
   * text the planner model authored (assistant text and every string in its tool-call arguments) is
     scanned at render time, and a hit raises ``TaintLeak``: the model can only know tainted text if
     something leaked, so the request fails closed.

System and user messages are not scanned: the system prompt is a constant, and user messages are the
user's own words (a user quoting an email to ATLAS is their choice).
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Final

from atlas.core.schema import strict_json_loads
from atlas.core.taint import TaintLeak, VariableStore, VarRef
from atlas.llm.interface import Message, Role, ToolCall


@dataclass(frozen=True, slots=True)
class TrustedText:
    """Text from a trusted origin: ATLAS's own code, the user, or a closed enum. Exact ``str`` only."""

    text: str

    def __post_init__(self) -> None:
        if type(self.text) is not str:
            raise TypeError("TrustedText takes an exact str; tainted values are passed as VarRef")


@dataclass(frozen=True, slots=True)
class AtlasText(TrustedText):
    """Text ATLAS generated from a constant template. Construct only through ``t()`` and ``gen()``."""


PromptPart = TrustedText | VarRef

# Values gen() may interpolate: numbers, enum values, ATLAS IDs, timestamps, configured contact names.
_SAFE_VALUE: Final = re.compile(r"[A-Za-z0-9 _.,:;/@+=()\[\]'#-]{0,128}")


def check_parts(parts: Iterable[object]) -> tuple[PromptPart, ...]:
    out: list[PromptPart] = []
    for part in parts:
        if not isinstance(part, TrustedText | VarRef):
            raise TypeError(f"planner prompts accept TrustedText and VarRef only, not {type(part).__name__}")
        out.append(part)
    return tuple(out)


def t(text: str) -> AtlasText:
    """A constant piece of ATLAS text. Tests require the argument to be a string literal."""
    return AtlasText(text)


def gen(template: str, **values: object) -> AtlasText:
    """ATLAS text from a literal template and constrained values (tests require a literal template)."""
    rendered: dict[str, str] = {}
    for name, value in values.items():
        if isinstance(value, bool):
            rendered[name] = "true" if value else "false"
        elif isinstance(value, int):
            rendered[name] = str(value)
        elif isinstance(value, str) and _SAFE_VALUE.fullmatch(value) is not None:
            rendered[name] = str.__str__(value)
        else:
            raise TypeError(f"gen() value {name!r} must be an int, a bool or a short plain string")
    return AtlasText(template.format(**rendered))


@dataclass(frozen=True, slots=True)
class _Entry:
    role: Role
    parts: tuple[PromptPart, ...]
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None


WITHHELD: Final = t("tool result withheld: TAINT_LEAK")


def string_leaves(value: Any) -> Iterator[str]:
    """Every string (keys included) inside parsed JSON."""
    stack = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, str):
            yield node
        elif isinstance(node, dict):
            for key, item in node.items():
                yield str(key)
                stack.append(item)
        elif isinstance(node, list):
            stack.extend(node)


def authored_texts(call: ToolCall) -> list[str]:
    """What the model wrote in a tool call: name, raw arguments, and each decoded string in them."""
    texts = [call.name, call.arguments_json]
    with contextlib.suppress(ValueError):  # malformed JSON: the raw text above is still scanned
        texts += list(string_leaves(strict_json_loads(call.arguments_json or "{}")))
    return texts


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

    def _scannable(self, parts: Iterable[PromptPart]) -> Iterator[str]:
        for part in parts:
            if isinstance(part, TrustedText) and not isinstance(part, AtlasText):
                yield part.text

    def add_tool_result(self, tool_call_id: str, parts: Iterable[object]) -> bool:
        """Add a tool result. If it carries tainted text, add a fixed refusal instead and return False."""
        checked = check_parts(parts)
        if any(self._store.index.contains(text) for text in self._scannable(checked)):
            self._entries.append(_Entry(Role.TOOL, (WITHHELD,), tool_call_id=tool_call_id))
            return False
        self._entries.append(_Entry(Role.TOOL, checked, tool_call_id=tool_call_id))
        return True

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
            scanned: list[str] = []
            if entry.role is Role.ASSISTANT:
                scanned = [p.text for p in entry.parts if isinstance(p, TrustedText)]
                for call in entry.tool_calls:
                    scanned += authored_texts(call)
            elif entry.role is Role.TOOL:
                scanned = list(self._scannable(entry.parts))  # re-checked: add_tool_result already withheld hits
            for text in scanned:
                if self._store.index.contains(text):
                    raise TaintLeak("tainted text reached a planner prompt")
            messages.append(Message(entry.role, self._text(entry.parts), entry.tool_calls, entry.tool_call_id))
        return messages
