"""LLMBackend contract, v1 (PLAN.md §5.8).

The orchestrator depends only on this module, never on a concrete server. Implementations:
``LMStudioBackend`` (OpenAI-compatible ``/v1/chat/completions`` + LM Studio REST),
``LlamaServerBackend`` and ``PiLocalBackend`` (the Pi fallback, intent routing only).

Rules every implementation follows:

* **Secrets stay at the transport layer.** API tokens are attached to HTTP requests by the
  implementation and never appear in messages, deltas, exceptions or logs.
* **No content logging.** Message text, tool arguments and model output are never logged. Log
  sizes, timings and model IDs only.
* **Deadlines are absolute** ``time.monotonic()`` values, not durations, so a caller's budget is
  shared across nested calls without drift.
* **Tool calls are proposals.** Implementations return them as data. Validating and dispatching
  them is the policy engine's job (§2.9), never the backend's.

Contract revision: 1.0.0. Changes follow contracts/README.md.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

CONTRACT_REVISION: Final = "1.0.0"


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A complete tool call proposed by the model. ``arguments_json`` is unvalidated model output."""

    id: str
    name: str
    arguments_json: str


@dataclass(frozen=True, slots=True)
class Message:
    role: Role
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None  # set when role is TOOL


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A tool offered to the model. ``parameters`` is a JSON Schema object."""

    name: str
    description: str
    parameters: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Capabilities:
    tools: bool
    json_schema: bool
    streaming: bool
    max_ctx: int


@dataclass(frozen=True, slots=True)
class Health:
    reachable: bool
    authenticated: bool
    model_loaded: str | None


class ReadinessStage(StrEnum):
    """Wake/readiness stages, in order. Same values as events.v1 ``WakeStage`` (§5.4)."""

    PACKET = "packet"  # host not answering on the network yet
    LINK = "link"  # WireGuard handshake / host reachable
    API = "api"  # server answers an authenticated GET /v1/models
    MODEL = "model"  # a 1-token completion with the target model succeeded


@dataclass(frozen=True, slots=True)
class Ready:
    """The only proof of readiness: a 1-token completion with ``model`` succeeded (§5.4)."""

    model: str
    warmup_ms: int


@dataclass(frozen=True, slots=True)
class NotReady:
    stage: ReadinessStage  # the last stage reached
    reason: str  # short, machine-oriented, contains no secrets or content


class FinishReason(StrEnum):
    STOP = "stop"
    LENGTH = "length"
    TOOL_CALLS = "tool_calls"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class Usage:
    prompt_tokens: int
    completion_tokens: int


@dataclass(frozen=True, slots=True)
class Delta:
    """One streamed increment.

    Text arrives in ``text`` fragments. Tool calls arrive only **complete** in ``tool_calls``
    (implementations buffer partial argument fragments). The final delta carries
    ``finish_reason`` and, where the server reports it, ``usage``.
    """

    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: FinishReason | None = None
    usage: Usage | None = None


@dataclass(frozen=True, slots=True)
class ModelInfo:
    id: str
    loaded: bool
    max_ctx: int | None = None
    size_bytes: int | None = None
    quantization: str | None = None
    extra: Mapping[str, str] = field(default_factory=dict)


# --- Errors -----------------------------------------------------------------------------------


class LLMError(Exception):
    """Base class. Messages must never contain prompt content or secrets."""


class BackendUnreachable(LLMError):
    """Network/link failure, or the server process is not running."""


class AuthenticationFailed(LLMError):
    """401/403 from the server, e.g. a rotated token (FI-7). Callers must not retry in a loop."""


class ModelNotLoaded(LLMError):
    """The requested model is not loaded and loading it was not permitted or failed."""


class DeadlineExceeded(LLMError):
    """The absolute deadline passed before the operation completed."""


class InvalidRequest(LLMError):
    """The backend rejected the request (bad schema, context too long, unsupported feature)."""


# --- The interface ----------------------------------------------------------------------------


class LLMBackend(abc.ABC):
    """An LLM server, reached however the implementation likes."""

    @abc.abstractmethod
    def id(self) -> str:
        """Stable identifier, e.g. ``lmstudio@10.77.0.2``."""

    @abc.abstractmethod
    def capabilities(self) -> Capabilities: ...

    @abc.abstractmethod
    async def health(self, deadline: float) -> Health:
        """Cheap check. Never loads a model, never raises for an unreachable server."""

    @abc.abstractmethod
    async def ensure_ready(self, model: str, deadline: float) -> Ready | NotReady:
        """Bring ``model`` to the MODEL stage, issuing the 1-token warm-up.

        Must not load a model when the caller's VRAM check failed; that check happens before this
        call (D16). Returns ``NotReady`` instead of raising when the deadline passes.
        """

    @abc.abstractmethod
    def chat(
        self,
        messages: Sequence[Message],
        *,
        max_tokens: int,
        tools: Sequence[ToolSpec] | None = None,
        response_schema: Mapping[str, Any] | None = None,
        deadline: float | None = None,
    ) -> AsyncIterator[Delta]:
        """Stream a completion. Non-streaming servers yield a single final ``Delta``.

        ``response_schema`` requests schema-constrained JSON output (quarantined reader, §2.9).
        Raises an ``LLMError`` subclass on failure; never yields partial tool calls.
        """

    @abc.abstractmethod
    async def list_models(self, deadline: float) -> Sequence[ModelInfo]: ...
