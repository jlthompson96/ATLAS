"""``LMStudioBackend``: the LLMBackend contract over LM Studio's OpenAI-compatible HTTP API.

Endpoints used (all on the desktop's ``10.77.0.2:1234`` through WireGuard, contracts/services.md):

* ``GET /v1/models``          authenticated liveness (the "api" wake stage)
* ``GET /api/v0/models``      LM Studio REST: which model is *loaded* (falls back to /v1/models)
* ``POST /v1/chat/completions`` streaming chat, tool calls, JSON-schema output, and the 1-token warm-up

Rules from the contract that this file enforces: the bearer token lives only in the request headers
(never in messages, exceptions, ``repr`` or logs), nothing here logs message content, deadlines are
absolute ``time.monotonic()`` values, and tool calls are returned complete and unvalidated.
Error mapping (HTTP -> exception): 401/403 ``AuthenticationFailed`` (no retry loop, FI-7), model
missing/unloadable ``ModelNotLoaded``, 4xx ``InvalidRequest``, connect failure/5xx/truncated stream
``BackendUnreachable``, deadline ``DeadlineExceeded``. Messages carry a status code or a short
machine word, never server text, because a server error can echo the prompt.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from atlas.llm.interface import (
    AuthenticationFailed,
    BackendUnreachable,
    Capabilities,
    DeadlineExceeded,
    Delta,
    FinishReason,
    Health,
    InvalidRequest,
    LLMBackend,
    LLMError,
    Message,
    ModelInfo,
    ModelNotLoaded,
    NotReady,
    ReadinessStage,
    Ready,
    Role,
    ToolCall,
    ToolSpec,
    Usage,
)
from atlas.net.http import Endpoint, HttpTransportError, fetch, parse_http_url, request
from atlas.net.http import HttpDeadlineError as _HttpDeadline

DEFAULT_BASE_URL: Final = "http://10.77.0.2:1234"
CREDENTIAL_NAME: Final = "lmstudio-api-token"
DEFAULT_REQUEST_TIMEOUT_S: Final = 120.0
HEALTH_TIMEOUT_S: Final = 3.0
MAX_JSON_BODY: Final = 4 * 1024 * 1024
MAX_SSE_LINE: Final = 1024 * 1024
MAX_TEXT_CHARS: Final = 1024 * 1024
MAX_TOOL_CALLS: Final = 32
MAX_TOOL_ARGS_CHARS: Final = 64 * 1024
MAX_TOOL_NAME_CHARS: Final = 128
WARMUP_PROMPT: Final = "ping"
# OpenAI-style ``reasoning_effort``. LM Studio 0.4.28 honours "none" for Gemma 4 E4B (no reasoning tokens);
# other values left it thinking in full (bench/results/B5-2026-11-25.md). None: the field is not sent.
REASONING_EFFORTS: Final = ("none", "low", "medium", "high")


class CredentialError(ValueError):
    """The token file is missing, unreadable or empty. The message never contains the token."""


def default_token_file() -> Path | None:
    """``$CREDENTIALS_DIRECTORY/lmstudio-api-token`` when run under systemd (``LoadCredentialEncrypted=``)."""
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    return Path(directory) / CREDENTIAL_NAME if directory else None


def read_token_file(path: Path | str) -> str:
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise CredentialError("cannot read the API token file") from None
    token = raw.strip()
    if not token:
        raise CredentialError("the API token file is empty")
    if any(c in token for c in "\r\n \t"):
        raise CredentialError("the API token file must hold one token and nothing else")
    return token


@dataclass
class _ToolBuffer:
    id: str = ""
    name: str = ""
    args: list[str] = field(default_factory=list)
    size: int = 0


def _finish(reason: object, have_calls: bool) -> FinishReason:
    if have_calls:
        return FinishReason.TOOL_CALLS
    return {
        "stop": FinishReason.STOP,
        "length": FinishReason.LENGTH,
        "tool_calls": FinishReason.TOOL_CALLS,
        "function_call": FinishReason.TOOL_CALLS,
    }.get(reason if isinstance(reason, str) else "", FinishReason.ERROR if reason else FinishReason.STOP)


def _looks_like_model_problem(body: bytes) -> bool:
    text = body[:4096].lower()
    return b"model" in text and any(w in text for w in (b"not found", b"not loaded", b"failed to load", b"load"))


def _raise_for_status(status: int, body: bytes) -> None:
    if status in (401, 403):
        raise AuthenticationFailed(f"HTTP {status}")
    if status in (400, 404, 422) and _looks_like_model_problem(body):
        raise ModelNotLoaded(f"HTTP {status}")
    if 400 <= status < 500 and status != 429:
        raise InvalidRequest(f"HTTP {status}")
    raise BackendUnreachable(f"HTTP {status}")


def _wire_message(m: Message) -> dict[str, Any]:
    out: dict[str, Any] = {"role": m.role.value, "content": m.content}
    if m.role is Role.ASSISTANT and m.tool_calls:
        out["tool_calls"] = [
            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments_json}}
            for c in m.tool_calls
        ]
    if m.role is Role.TOOL and m.tool_call_id is not None:
        out["tool_call_id"] = m.tool_call_id
    return out


class LMStudioBackend(LLMBackend):
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        token: str | None = None,
        model: str | None = None,
        max_ctx: int = 16_384,
        request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
        reasoning_effort: str | None = None,
    ) -> None:
        if reasoning_effort is not None and reasoning_effort not in REASONING_EFFORTS:
            raise ValueError(f"reasoning_effort must be one of {', '.join(REASONING_EFFORTS)}")
        self._endpoint: Endpoint = parse_http_url(base_url)
        self._headers: dict[str, str] = {"Accept": "application/json"}
        if token is not None:
            self._headers["Authorization"] = f"Bearer {token}"
        self._model = model
        self._max_ctx = max_ctx
        self._timeout = request_timeout_s
        self._reasoning_effort = reasoning_effort

    @classmethod
    def from_token_file(cls, base_url: str, token_file: Path | str | None, **kwargs: Any) -> LMStudioBackend:
        token = read_token_file(token_file) if token_file is not None else None
        return cls(base_url, token=token, **kwargs)

    def __repr__(self) -> str:  # never show headers
        return f"LMStudioBackend({self.id()})"

    # --- LLMBackend ---------------------------------------------------------------------------

    def id(self) -> str:
        return f"lmstudio@{self._endpoint.host}"

    def capabilities(self) -> Capabilities:
        return Capabilities(tools=True, json_schema=True, streaming=True, max_ctx=self._max_ctx)

    async def health(self, deadline: float) -> Health:
        deadline = min(deadline, time.monotonic() + HEALTH_TIMEOUT_S)
        try:
            status, _ = await self._get("/v1/models", deadline)
        except LLMError:
            return Health(False, False, None)
        if status in (401, 403):
            return Health(True, False, None)
        if status != 200:
            return Health(True, False, None)
        return Health(True, True, await self._loaded_model(deadline))

    async def ensure_ready(self, model: str, deadline: float) -> Ready | NotReady:
        health = await self.health(deadline)
        if not health.reachable:
            return NotReady(ReadinessStage.PACKET, "unreachable")
        if not health.authenticated:
            return NotReady(ReadinessStage.API, "auth_failed")
        started = time.monotonic()
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": WARMUP_PROMPT}],
            "max_tokens": 1,
            "stream": False,
            "temperature": 0,
        }
        try:
            status, body = await self._post("/v1/chat/completions", payload, deadline)
            if status != 200:
                _raise_for_status(status, body)
            if not isinstance(json.loads(body).get("choices"), list):
                return NotReady(ReadinessStage.MODEL, "bad_response")
        except AuthenticationFailed:
            return NotReady(ReadinessStage.API, "auth_failed")
        except ModelNotLoaded:
            return NotReady(ReadinessStage.MODEL, "model_not_found")
        except DeadlineExceeded:
            return NotReady(ReadinessStage.MODEL, "deadline")
        except InvalidRequest:
            return NotReady(ReadinessStage.MODEL, "invalid_request")
        except BackendUnreachable:
            return NotReady(ReadinessStage.MODEL, "server_error")
        except (ValueError, AttributeError):
            return NotReady(ReadinessStage.MODEL, "bad_response")
        self._model = model
        return Ready(model, int((time.monotonic() - started) * 1000))

    async def list_models(self, deadline: float) -> Sequence[ModelInfo]:
        status, body = await self._get("/api/v0/models", deadline)
        if status == 200:
            return self._parse_v0(body)
        if status in (401, 403):
            raise AuthenticationFailed(f"HTTP {status}")
        status, body = await self._get("/v1/models", deadline)
        if status != 200:
            _raise_for_status(status, body)
        try:
            return [ModelInfo(str(m["id"]), loaded=False) for m in json.loads(body)["data"]]
        except (ValueError, KeyError, TypeError):
            raise BackendUnreachable("bad model list") from None

    async def chat(
        self,
        messages: Sequence[Message],
        *,
        max_tokens: int,
        tools: Sequence[ToolSpec] | None = None,
        response_schema: Mapping[str, Any] | None = None,
        deadline: float | None = None,
    ) -> AsyncIterator[Delta]:
        if deadline is None:
            deadline = time.monotonic() + self._timeout
        payload: dict[str, Any] = {
            "messages": [_wire_message(m) for m in messages],
            "max_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if self._model is not None:
            payload["model"] = self._model
        if self._reasoning_effort is not None:
            payload["reasoning_effort"] = self._reasoning_effort
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
                }
                for t in tools
            ]
        if response_schema is not None:
            payload["temperature"] = 0
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "reply", "strict": True, "schema": response_schema},
            }
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = {**self._headers, "Content-Type": "application/json", "Accept": "text/event-stream"}

        buffers: dict[int, _ToolBuffer] = {}
        finish_raw: object = None
        usage: Usage | None = None
        text_chars = 0
        saw_done = False
        try:
            async with request(
                self._endpoint, "POST", "/v1/chat/completions", headers=headers, body=body, deadline=deadline
            ) as resp:
                if resp.status != 200:
                    _raise_for_status(resp.status, await resp.read(64 * 1024))
                pending = b""
                async for chunk in resp.chunks():
                    pending += chunk
                    while (nl := pending.find(b"\n")) >= 0:
                        line, pending = pending[:nl], pending[nl + 1 :]
                        data = self._sse_data(line)
                        if data is None:
                            continue
                        if data == "[DONE]":
                            saw_done = True
                            break
                        obj = self._parse_event(data)
                        if (u := obj.get("usage")) is not None:
                            usage = _usage(u)
                        for choice in (obj.get("choices") or [])[:1]:
                            delta = choice.get("delta") or {}
                            text = delta.get("content")
                            if isinstance(text, str) and text:
                                text_chars += len(text)
                                if text_chars > MAX_TEXT_CHARS:
                                    raise InvalidRequest("response too long")
                                yield Delta(text=text)
                            self._buffer_tool_calls(buffers, delta.get("tool_calls"))
                            if choice.get("finish_reason"):
                                finish_raw = choice["finish_reason"]
                    if saw_done:
                        break
                    if len(pending) > MAX_SSE_LINE:
                        raise InvalidRequest("stream line too long")
        except _HttpDeadline:
            raise DeadlineExceeded("deadline passed") from None
        except HttpTransportError:
            raise BackendUnreachable("connection failed") from None
        if finish_raw is None and not saw_done:
            raise BackendUnreachable("stream ended early")
        calls = tuple(
            ToolCall(b.id or f"call_{i}", b.name, "".join(b.args) or "{}") for i, b in sorted(buffers.items())
        )
        yield Delta(tool_calls=calls, finish_reason=_finish(finish_raw, bool(calls)), usage=usage)

    # --- helpers ------------------------------------------------------------------------------

    async def _get(self, path: str, deadline: float) -> tuple[int, bytes]:
        try:
            return await fetch(
                self._endpoint, "GET", path, headers=self._headers, deadline=deadline, max_body=MAX_JSON_BODY
            )
        except _HttpDeadline:
            raise DeadlineExceeded("deadline passed") from None
        except HttpTransportError:
            raise BackendUnreachable("connection failed") from None

    async def _post(self, path: str, payload: Mapping[str, Any], deadline: float) -> tuple[int, bytes]:
        headers = {**self._headers, "Content-Type": "application/json"}
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        try:
            return await fetch(
                self._endpoint, "POST", path, headers=headers, body=body, deadline=deadline, max_body=MAX_JSON_BODY
            )
        except _HttpDeadline:
            raise DeadlineExceeded("deadline passed") from None
        except HttpTransportError:
            raise BackendUnreachable("connection failed") from None

    async def _loaded_model(self, deadline: float) -> str | None:
        try:
            status, body = await self._get("/api/v0/models", deadline)
            if status != 200:
                return None
            loaded = [m for m in self._parse_v0(body) if m.loaded and m.extra.get("type", "llm") in ("llm", "vlm")]
        except LLMError:
            return None
        for m in loaded:
            if m.id == self._model:
                return m.id
        return loaded[0].id if loaded else None

    @staticmethod
    def _parse_v0(body: bytes) -> list[ModelInfo]:
        try:
            out = []
            for m in json.loads(body)["data"]:
                ctx = m.get("loaded_context_length") or m.get("max_context_length")
                extra = {k: str(m[k])[:64] for k in ("type", "arch", "publisher") if k in m}
                out.append(
                    ModelInfo(
                        str(m["id"]),
                        loaded=m.get("state") == "loaded",
                        max_ctx=ctx if isinstance(ctx, int) else None,
                        quantization=str(m["quantization"]) if m.get("quantization") else None,
                        extra=extra,
                    )
                )
            return out
        except (ValueError, KeyError, TypeError, AttributeError):
            raise BackendUnreachable("bad model list") from None

    @staticmethod
    def _sse_data(line: bytes) -> str | None:
        text = line.decode("utf-8", "replace").rstrip("\r")
        if not text.startswith("data:"):
            return None
        return text[5:].strip() or None

    @staticmethod
    def _parse_event(data: str) -> dict[str, Any]:
        try:
            obj = json.loads(data)
        except ValueError:
            raise BackendUnreachable("malformed stream") from None
        if not isinstance(obj, dict):
            raise BackendUnreachable("malformed stream")
        if "error" in obj:
            if _looks_like_model_problem(json.dumps(obj["error"]).encode()):
                raise ModelNotLoaded("stream error")
            raise InvalidRequest("stream error")
        return obj

    @staticmethod
    def _buffer_tool_calls(buffers: dict[int, _ToolBuffer], fragments: object) -> None:
        if not isinstance(fragments, list):
            return
        for position, frag in enumerate(fragments):
            if not isinstance(frag, dict):
                continue
            index = frag.get("index")
            buf = buffers.setdefault(index if isinstance(index, int) else position, _ToolBuffer())
            if len(buffers) > MAX_TOOL_CALLS:
                raise InvalidRequest("too many tool calls")
            if isinstance(frag.get("id"), str) and not buf.id:
                buf.id = frag["id"][:128]
            fn = frag.get("function") or {}
            if isinstance(fn.get("name"), str) and not buf.name:
                buf.name = fn["name"][:MAX_TOOL_NAME_CHARS]
            args = fn.get("arguments")
            if args is not None and not isinstance(args, str):
                args = json.dumps(args)
            if args:
                buf.size += len(args)
                if buf.size > MAX_TOOL_ARGS_CHARS:
                    raise InvalidRequest("tool arguments too long")
                buf.args.append(args)


def _usage(raw: object) -> Usage | None:
    if not isinstance(raw, dict):
        return None
    p, c = raw.get("prompt_tokens"), raw.get("completion_tokens")
    if isinstance(p, int) and isinstance(c, int):
        return Usage(p, c)
    return None
