"""Taint and provenance for untrusted content (PLAN.md §2.9, rule 1 and 2).

Three value types carry data that came from third parties:

* ``UntrustedContent``: one raw item (an email, a Discord message, a calendar event, a web page) as
  an integration delivered it.
* ``TaintedStr``: text derived from untrusted content (quarantined-reader output, a subject line,
  memory written from an email).
* Both are bound into a ``VariableStore`` and handed around as opaque ``VarRef`` names (``$EMAIL_1``).

Neither ``UntrustedContent`` nor ``TaintedStr`` is a ``str`` and neither converts to one: ``str()``,
``format()``, f-strings, ``%``, ``bytes()``, pickling and copying all raise ``TaintError``. The planner
prompt builder (``atlas.core.prompt``) accepts only ``TrustedText`` and ``VarRef`` parts, and
``TrustedText`` accepts only an exact ``str``, so tainted data cannot be put into a planner prompt by
accident. The one escape hatch, ``_reveal()``, is for display, payload building and the quarantined
reader; ``tests/test_core_taint.py`` pins the modules allowed to call it, and the prompt builder
scans every rendered prompt against the store's taint index as a second, independent layer.

Taint propagates: ``TaintedStr + str``, ``str + TaintedStr`` and ``derive()`` return a ``TaintedStr``
whose provenance is the union of its inputs'.
"""

from __future__ import annotations

import hashlib
import itertools
import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final, NoReturn, final


class TaintError(TypeError):
    """Something tried to turn tainted data into plain text outside the allowed paths."""


class TaintLeak(RuntimeError):
    """Tainted text was found in a planner prompt. The request must fail closed."""


class UndefinedVariable(KeyError):
    """A ``$VAR`` name that is not bound in the store."""


class SourceKind(StrEnum):
    """events.v1 ``SourceRef.kind``."""

    USER = "user"
    EMAIL = "email"
    DISCORD = "discord"
    CALENDAR = "calendar"
    WEB = "web"
    MEMORY = "memory"
    MARKET = "market"
    SYSTEM = "system"


TRUSTED_SOURCE_KINDS: Final = frozenset({SourceKind.USER, SourceKind.SYSTEM})

# Audit DataRef.resource_id is ``^[a-z][a-z0-9_]*:[^\s]{1,300}$`` and events SourceRef.ref is at most 200
# characters; ATLAS-generated IDs use this stricter subset so they are safe to show the planner.
RESOURCE_ID_RE: Final = re.compile(r"[a-z][a-z0-9_]*:[A-Za-z0-9_.:/@+=-]{1,180}")


@dataclass(frozen=True, slots=True, order=True)
class Source:
    """Where a piece of data came from. ``resource_id`` uses the audit ``<namespace>:<path>`` form."""

    kind: SourceKind
    resource_id: str
    observed_at_ms: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.kind, SourceKind):
            raise TypeError("Source.kind must be a SourceKind")
        if RESOURCE_ID_RE.fullmatch(self.resource_id) is None:
            raise ValueError("Source.resource_id must look like '<namespace>:<path>'")
        if isinstance(self.observed_at_ms, bool) or not isinstance(self.observed_at_ms, int) or self.observed_at_ms < 0:
            raise ValueError("Source.observed_at_ms must be a non-negative integer")

    @property
    def tainted(self) -> bool:
        return self.kind not in TRUSTED_SOURCE_KINDS


MAX_EVENT_SOURCES: Final = 20  # events.v1 Provenance.sources maxItems


@dataclass(frozen=True, slots=True)
class Provenance:
    sources: frozenset[Source] = frozenset()

    @classmethod
    def of(cls, *sources: Source) -> Provenance:
        return cls(frozenset(sources))

    @property
    def tainted(self) -> bool:
        return any(s.tainted for s in self.sources)

    def union(self, *others: Provenance) -> Provenance:
        merged = set(self.sources)
        for other in others:
            merged |= other.sources
        return Provenance(frozenset(merged))

    def tainted_sources(self) -> list[Source]:
        return sorted(s for s in self.sources if s.tainted)

    def to_event(self) -> dict[str, Any]:
        """events.v1 ``Provenance``: tainted flag plus up to 20 sources (tainted ones first)."""
        seen: set[tuple[str, str]] = set()
        refs: list[dict[str, Any]] = []
        for src in sorted(self.sources, key=lambda s: (not s.tainted, s.kind.value, s.resource_id)):
            key = (src.kind.value, src.resource_id)
            if key in seen:
                continue
            seen.add(key)
            refs.append({"kind": src.kind.value, "ref": src.resource_id})
            if len(refs) == MAX_EVENT_SOURCES:
                break
        return {"tainted": self.tainted, "sources": refs}


EMPTY_PROVENANCE: Final = Provenance()


def _refuse(*_: object) -> NoReturn:
    raise TaintError("tainted data cannot be converted to plain text; pass it by $VAR reference")


@final
class TaintedStr:
    """Text derived from untrusted content. Not a ``str``; never converts to one implicitly."""

    __slots__ = ("__value", "_provenance")

    def __init__(self, value: str, provenance: Provenance) -> None:
        if type(value) is not str:
            raise TypeError("TaintedStr wraps a plain str")
        if not provenance.tainted:
            raise ValueError("TaintedStr needs at least one untrusted source")
        self.__value = value
        self._provenance = provenance

    @property
    def provenance(self) -> Provenance:
        return self._provenance

    def __len__(self) -> int:
        return len(self.__value)

    @property
    def byte_len(self) -> int:
        return len(self.__value.encode("utf-8", "surrogatepass"))

    def sha256(self) -> str:
        return hashlib.sha256(self.__value.encode("utf-8", "surrogatepass")).hexdigest()

    # --- every implicit conversion is refused ---------------------------------------------------
    __str__ = _refuse
    __bytes__ = _refuse
    __format__ = _refuse
    __reduce__ = _refuse
    __reduce_ex__ = _refuse
    __copy__ = _refuse
    __deepcopy__ = _refuse
    __iter__ = _refuse
    __getitem__ = _refuse
    __contains__ = _refuse
    __bool__ = _refuse

    def __repr__(self) -> str:
        return f"<TaintedStr {len(self.__value)} chars from {len(self._provenance.sources)} source(s)>"

    # --- propagation ------------------------------------------------------------------------------
    def __add__(self, other: TaintedStr | str) -> TaintedStr:
        if isinstance(other, TaintedStr):
            return TaintedStr(self.__value + other.__value, self._provenance.union(other._provenance))
        if type(other) is str:
            return TaintedStr(self.__value + other, self._provenance)
        return NotImplemented

    def __radd__(self, other: str) -> TaintedStr:
        if type(other) is str:
            return TaintedStr(other + self.__value, self._provenance)
        return NotImplemented

    def derive(self, fn: Callable[[str], str], *also: TaintedStr) -> TaintedStr:
        """Apply ``fn`` to the text; the result keeps (the union of) the provenance."""
        provenance = self._provenance.union(*(t._provenance for t in also))
        result = fn(self.__value)
        if type(result) is not str:
            raise TypeError("derive() functions must return a plain str")
        return TaintedStr(result, provenance)

    def _reveal(self) -> str:
        """The plain text. Allowed callers are pinned by tests/test_core_taint.py."""
        return self.__value


def tainted_join(parts: Iterable[TaintedStr | str], sep: str = "") -> TaintedStr | str:
    """Join text, returning a ``TaintedStr`` if any part is tainted (taint propagates)."""
    out: TaintedStr | str = ""
    for i, part in enumerate(parts):
        if i:
            out = out + sep
        out = out + part
    return out


@final
class UntrustedContent:
    """One raw third-party item, as an integration service delivered it.

    The raw bytes are reachable only through ``_raw_bytes()`` (the quarantined reader's sanitizer).
    Metadata that is attacker-controlled (sender address, display name, subject) is ``TaintedStr``.
    """

    __slots__ = ("__raw", "media_type", "sender", "source", "subject", "unread")

    MEDIA_TYPES: Final = frozenset({"text/plain", "text/html", "text/calendar"})

    def __init__(
        self,
        raw: bytes,
        *,
        source: Source,
        media_type: str = "text/plain",
        sender: str | None = None,
        subject: str | None = None,
        unread: bool = True,
    ) -> None:
        if type(raw) is not bytes:
            raise TypeError("UntrustedContent wraps raw bytes")
        if not source.tainted:
            raise ValueError("UntrustedContent must come from an untrusted source kind")
        if media_type not in self.MEDIA_TYPES:
            raise ValueError("unsupported media type")
        prov = Provenance.of(source)
        self.__raw = raw
        self.source = source
        self.media_type = media_type
        self.sender = None if sender is None else TaintedStr(sender, prov)
        self.subject = None if subject is None else TaintedStr(subject, prov)
        self.unread = unread

    @property
    def provenance(self) -> Provenance:
        return Provenance.of(self.source)

    @property
    def byte_len(self) -> int:
        return len(self.__raw)

    def sha256(self) -> str:
        return hashlib.sha256(self.__raw).hexdigest()

    __str__ = _refuse
    __bytes__ = _refuse
    __format__ = _refuse
    __reduce__ = _refuse
    __reduce_ex__ = _refuse
    __copy__ = _refuse
    __deepcopy__ = _refuse

    def __repr__(self) -> str:
        return f"<UntrustedContent {self.source.resource_id} {len(self.__raw)} bytes>"

    def _raw_bytes(self) -> bytes:
        """The raw item. Allowed callers are pinned by tests/test_core_taint.py."""
        return self.__raw


# --- variable references -------------------------------------------------------------------------

VAR_NAME_RE: Final = re.compile(r"\$[A-Z]{1,16}_[1-9][0-9]{0,5}")
# Anything that looks like a reference embedded inside other text (refused: no guessing, §2.9).
EMBEDDED_VAR_RE: Final = re.compile(r"\$[A-Z]{1,16}_[0-9]+")
_PREFIX_RE: Final = re.compile(r"[A-Z]{1,16}")


@dataclass(frozen=True, slots=True)
class VarRef:
    """An opaque name for a bound value, e.g. ``$SUMMARY_3``. Carries no content."""

    name: str

    def __post_init__(self) -> None:
        if type(self.name) is not str or VAR_NAME_RE.fullmatch(self.name) is None:
            raise ValueError("not a variable name")


@dataclass(frozen=True, slots=True)
class Variable:
    ref: VarRef
    value: TaintedStr | UntrustedContent
    label: str  # trusted description chosen by ATLAS code, e.g. "email summary"
    created_at_ms: int

    @property
    def provenance(self) -> Provenance:
        return self.value.provenance

    @property
    def is_item(self) -> bool:
        return isinstance(self.value, UntrustedContent)


# --- leak detection --------------------------------------------------------------------------------

LEAK_WINDOW: Final = 24  # characters, after normalization
RAW_STRIDE: Final = 8  # large raw items are indexed every 8 chars: any copied run of >= 31 chars is caught
SMALL_VALUE_LIMIT: Final = 4096  # values up to this size are indexed at every position
RAW_INDEX_LIMIT: Final = 1024 * 1024  # characters of a raw item that are indexed
_WS_RE: Final = re.compile(r"\s+")


def _normalize(text: str) -> str:
    return _WS_RE.sub(" ", text.casefold()).strip()


@dataclass
class TaintIndex:
    """Fingerprints of every tainted value bound in a store, used to scan planner prompts.

    Windows that occur in the trusted corpus (system prompt, tool descriptions) are never flagged, so
    untrusted content cannot make the planner refuse to run by quoting its own instructions.
    """

    windows: set[int] = field(default_factory=set)
    trusted: set[int] = field(default_factory=set)

    def add_trusted_corpus(self, text: str) -> None:
        norm = _normalize(text)
        self.trusted.update(hash(norm[i : i + LEAK_WINDOW]) for i in range(max(0, len(norm) - LEAK_WINDOW + 1)))

    def add(self, text: str) -> None:
        norm = _normalize(text[:RAW_INDEX_LIMIT])
        if len(norm) < LEAK_WINDOW:
            if len(norm) >= 12:  # short values: index the whole value
                self.windows.add(hash(norm))
            return
        stride = 1 if len(norm) <= SMALL_VALUE_LIMIT else RAW_STRIDE
        for i in range(0, len(norm) - LEAK_WINDOW + 1, stride):
            self.windows.add(hash(norm[i : i + LEAK_WINDOW]))

    def _short_hit(self, norm: str) -> bool:
        # Short tainted values (12..23 chars) are matched as whole substrings.
        for size in range(12, LEAK_WINDOW):
            for i in range(len(norm) - size + 1):
                h = hash(norm[i : i + size])
                if h in self.windows and h not in self.trusted:
                    return True
        return False

    def contains(self, text: str) -> bool:
        norm = _normalize(text)
        for i in range(len(norm) - LEAK_WINDOW + 1):
            h = hash(norm[i : i + LEAK_WINDOW])
            if h in self.windows and h not in self.trusted:
                return True
        return self._short_hit(norm)


class VariableStore:
    """Binds tainted values to ``$VAR`` names for one session. Never hands content to the planner."""

    MAX_VARIABLES: Final = 4096

    def __init__(self, clock_ms: Callable[[], int]) -> None:
        self._clock = clock_ms
        self._vars: dict[str, Variable] = {}
        self._counters: dict[str, Iterator[int]] = {}
        self._items_by_resource: dict[str, VarRef] = {}
        self.index = TaintIndex()

    def __len__(self) -> int:
        return len(self._vars)

    def _next(self, prefix: str) -> VarRef:
        if _PREFIX_RE.fullmatch(prefix) is None:
            raise ValueError("variable prefix must be 1-16 upper-case letters")
        if len(self._vars) >= self.MAX_VARIABLES:
            raise OverflowError("too many variables in this session")
        counter = self._counters.setdefault(prefix, itertools.count(1))
        return VarRef(f"${prefix}_{next(counter)}")

    def bind_text(self, prefix: str, value: TaintedStr, label: str) -> VarRef:
        if not isinstance(value, TaintedStr):
            raise TypeError("only tainted text is bound; trusted text goes straight into the prompt")
        ref = self._next(prefix)
        self._vars[ref.name] = Variable(ref, value, label, self._clock())
        self.index.add(value._reveal())
        return ref

    def bind_item(self, prefix: str, item: UntrustedContent, label: str) -> VarRef:
        existing = self._items_by_resource.get(item.source.resource_id)
        if existing is not None:
            return existing
        ref = self._next(prefix)
        self._vars[ref.name] = Variable(ref, item, label, self._clock())
        self._items_by_resource[item.source.resource_id] = ref
        self.index.add(item._raw_bytes().decode("utf-8", "replace"))
        for meta in (item.sender, item.subject):
            if meta is not None:
                self.index.add(meta._reveal())
        return ref

    def get(self, ref: VarRef | str) -> Variable:
        name = ref.name if isinstance(ref, VarRef) else ref
        var = self._vars.get(name)
        if var is None:
            raise UndefinedVariable(name)
        return var

    def lookup(self, name: str) -> Variable | None:
        return self._vars.get(name)

    def describe(self, ref: VarRef) -> str:
        """A trusted description of a variable: label, source kinds and IDs, size. Never content."""
        var = self.get(ref)
        sources = var.provenance.tainted_sources()
        where = ", ".join(f"{s.kind.value} {s.resource_id}" for s in sources[:3])
        if len(sources) > 3:
            where += f" (+{len(sources) - 3} more)"
        size = var.value.byte_len
        return f"{ref.name} [tainted {var.label}, {size} bytes, from {where}]"
