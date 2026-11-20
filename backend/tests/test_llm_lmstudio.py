"""LMStudioBackend against a fake LM Studio on 127.0.0.1 (no real model, no real network)."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest

from atlas.llm.interface import (
    AuthenticationFailed,
    BackendUnreachable,
    DeadlineExceeded,
    Delta,
    FinishReason,
    InvalidRequest,
    Message,
    ModelNotLoaded,
    NotReady,
    ReadinessStage,
    Ready,
    Role,
    ToolCall,
    ToolSpec,
)
from atlas.llm.lmstudio import CredentialError, LMStudioBackend, default_token_file, read_token_file
from tests.net_support import TOKEN, FakeServer, Reply, Request, closed_port, sse

MODEL = "qwen-test-9b"


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def models_reply(loaded: bool = True) -> Reply:
    return Reply.of_json(
        {
            "data": [
                {
                    "id": MODEL,
                    "type": "llm",
                    "state": "loaded" if loaded else "not-loaded",
                    "max_context_length": 32768,
                    "loaded_context_length": 16384,
                    "quantization": "Q4_K_M",
                },
                {"id": "embed-x", "type": "embeddings", "state": "not-loaded"},
            ]
        }
    )


def lmstudio(
    chat: Callable[[Request], Reply] | None = None, *, loaded: bool = True, v0: bool = True, token: str = TOKEN
) -> Callable[[Request], Reply]:
    def handle(req: Request) -> Reply:
        if req.headers.get("authorization") != f"Bearer {token}":
            return Reply.of_json({"error": {"message": "invalid api key"}}, 401)
        if req.path == "/v1/models":
            return Reply.of_json({"data": [{"id": MODEL}, {"id": "embed-x"}]})
        if req.path == "/api/v0/models":
            return models_reply(loaded) if v0 else Reply.of_json({"error": "nope"}, 404)
        if req.path == "/v1/chat/completions" and chat is not None:
            return chat(req)
        return Reply.of_json({"error": "unexpected"}, 404)

    return handle


def deadline(seconds: float = 5.0) -> float:
    return time.monotonic() + seconds


async def collect(stream: AsyncIterator[Delta]) -> list[Delta]:
    return [d async for d in stream]


def text_stream(*pieces: str, finish: str = "stop") -> Reply:
    events: list[Any] = [{"choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]}]
    events += [{"choices": [{"index": 0, "delta": {"content": p}}]} for p in pieces]
    events += [
        {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
        {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}},
        "[DONE]",
    ]
    return Reply(chunks=sse(*events), headers={"Content-Type": "text/event-stream"})


async def with_server(
    handler: Callable[[Request], Reply], body: Callable[[FakeServer, LMStudioBackend], Coroutine[Any, Any, Any]]
) -> Any:
    server = await FakeServer(handler).start()
    try:
        return await body(server, LMStudioBackend(server.url, token=TOKEN, model=MODEL))
    finally:
        await server.close()


USER = [Message(Role.USER, "hello there")]


# --- health, readiness, models ----------------------------------------------------------------------------


def test_health_reports_the_loaded_model_when_authenticated() -> None:
    async def body(server: FakeServer, backend: LMStudioBackend) -> None:
        health = await backend.health(deadline())
        assert (health.reachable, health.authenticated, health.model_loaded) == (True, True, MODEL)
        assert all(r.headers["authorization"] == f"Bearer {TOKEN}" for r in server.requests)

    run(with_server(lmstudio(), body))


def test_health_with_nothing_loaded_and_without_the_rest_api() -> None:
    async def body(server: FakeServer, backend: LMStudioBackend) -> None:
        assert (await backend.health(deadline())).model_loaded is None

    run(with_server(lmstudio(loaded=False), body))
    run(with_server(lmstudio(v0=False), body))


def test_health_distinguishes_bad_token_from_unreachable_and_never_raises() -> None:
    async def bad_token() -> None:
        server = await FakeServer(lmstudio()).start()
        try:
            h = await LMStudioBackend(server.url, token="wrong").health(deadline())  # noqa: S106
            assert (h.reachable, h.authenticated, h.model_loaded) == (True, False, None)
        finally:
            await server.close()

    run(bad_token())
    h = run(LMStudioBackend(f"http://127.0.0.1:{closed_port()}", token=TOKEN).health(deadline()))
    assert (h.reachable, h.authenticated) == (False, False)


def test_health_gives_up_on_a_silent_server_within_the_health_budget() -> None:
    async def body(server: FakeServer, backend: LMStudioBackend) -> None:
        started = time.monotonic()
        h = await backend.health(deadline(0.3))
        assert not h.reachable and time.monotonic() - started < 2

    run(with_server(lambda r: Reply(stall=True), body))


def test_ensure_ready_sends_a_one_token_completion_and_reports_warmup() -> None:
    def chat(req: Request) -> Reply:
        return Reply.of_json(
            {"choices": [{"message": {"role": "assistant", "content": "p"}, "finish_reason": "length"}]}
        )

    async def body(server: FakeServer, backend: LMStudioBackend) -> None:
        result = await backend.ensure_ready(MODEL, deadline())
        assert isinstance(result, Ready) and result.model == MODEL and result.warmup_ms >= 0
        sent = next(r for r in server.requests if r.path == "/v1/chat/completions").json()
        assert sent["max_tokens"] == 1 and sent["model"] == MODEL and sent["stream"] is False

    run(with_server(lmstudio(chat), body))


@pytest.mark.parametrize(
    ("reply", "stage", "reason"),
    [
        (Reply.of_json({"error": {"message": "Model not found"}}, 404), ReadinessStage.MODEL, "model_not_found"),
        (Reply.of_json({"error": {"message": "context too long"}}, 400), ReadinessStage.MODEL, "invalid_request"),
        (Reply.of_json({"oops": 1}), ReadinessStage.MODEL, "bad_response"),
        (Reply.of_json({"error": "boom"}, 500), ReadinessStage.MODEL, "server_error"),
    ],
)
def test_ensure_ready_failures_are_not_ready_with_a_stage_and_reason(
    reply: Reply, stage: ReadinessStage, reason: str
) -> None:
    async def body(server: FakeServer, backend: LMStudioBackend) -> None:
        result = await backend.ensure_ready(MODEL, deadline())
        assert isinstance(result, NotReady) and (result.stage, result.reason) == (stage, reason)

    run(with_server(lmstudio(lambda r: reply), body))


def test_ensure_ready_stage_for_unreachable_auth_and_deadline() -> None:
    unreachable = run(LMStudioBackend(f"http://127.0.0.1:{closed_port()}", token=TOKEN).ensure_ready(MODEL, deadline()))
    assert unreachable == NotReady(ReadinessStage.PACKET, "unreachable")

    async def wrong_token(server: FakeServer, _: LMStudioBackend) -> NotReady | Ready:
        return await LMStudioBackend(server.url, token="wrong").ensure_ready(MODEL, deadline())  # noqa: S106

    assert run(with_server(lmstudio(), wrong_token)) == NotReady(ReadinessStage.API, "auth_failed")

    async def slow(server: FakeServer, backend: LMStudioBackend) -> NotReady | Ready:
        return await backend.ensure_ready(MODEL, deadline(0.5))

    assert run(with_server(lmstudio(lambda r: Reply(stall=True)), slow)) == NotReady(ReadinessStage.MODEL, "deadline")


def test_list_models_prefers_the_lmstudio_rest_api_and_falls_back() -> None:
    async def rest(server: FakeServer, backend: LMStudioBackend) -> None:
        models = await backend.list_models(deadline())
        assert next((m.id, m.loaded, m.max_ctx, m.quantization) for m in models) == (MODEL, True, 16384, "Q4_K_M")

    run(with_server(lmstudio(), rest))

    async def plain(server: FakeServer, backend: LMStudioBackend) -> None:
        models = await backend.list_models(deadline())
        assert [m.id for m in models] == [MODEL, "embed-x"] and not any(m.loaded for m in models)

    run(with_server(lmstudio(v0=False), plain))


def test_list_models_auth_error() -> None:
    async def body(server: FakeServer, _: LMStudioBackend) -> None:
        with pytest.raises(AuthenticationFailed):
            await LMStudioBackend(server.url, token="wrong").list_models(deadline())  # noqa: S106

    run(with_server(lmstudio(), body))


# --- chat: streaming --------------------------------------------------------------------------------------------


def test_chat_streams_text_then_a_final_delta_with_finish_reason_and_usage() -> None:
    async def body(server: FakeServer, backend: LMStudioBackend) -> None:
        deltas = await collect(backend.chat(USER, max_tokens=64, deadline=deadline()))
        assert [d.text for d in deltas[:-1]] == ["Hel", "lo", "!"]
        last = deltas[-1]
        assert last.finish_reason is FinishReason.STOP and last.tool_calls == ()
        assert (last.usage.prompt_tokens, last.usage.completion_tokens) == (12, 3)  # type: ignore[union-attr]
        sent = server.requests[-1].json()
        assert sent["stream"] is True and sent["max_tokens"] == 64 and sent["model"] == MODEL
        assert sent["messages"] == [{"role": "user", "content": "hello there"}]
        assert "tools" not in sent and "response_format" not in sent

    run(with_server(lmstudio(lambda r: text_stream("Hel", "lo", "!")), body))


def test_chat_length_finish_is_reported() -> None:
    async def body(server: FakeServer, backend: LMStudioBackend) -> None:
        deltas = await collect(backend.chat(USER, max_tokens=2, deadline=deadline()))
        assert deltas[-1].finish_reason is FinishReason.LENGTH

    run(with_server(lmstudio(lambda r: text_stream("ab", finish="length")), body))


def test_chat_buffers_fragmented_tool_calls_and_returns_them_complete() -> None:
    def fragments(req: Request) -> Reply:
        def tc(index: int, **fn: Any) -> dict[str, Any]:
            call: dict[str, Any] = {"index": index, "function": fn}
            return {"choices": [{"index": 0, "delta": {"tool_calls": [call]}}]}

        first = tc(0, name="get_time", arguments="")
        first["choices"][0]["delta"]["tool_calls"][0].update(id="call_a", type="function")
        second = tc(1, name="search_memory", arguments='{"query":')
        second["choices"][0]["delta"]["tool_calls"][0].update(id="call_b", type="function")
        events: list[Any] = [
            first,
            second,
            tc(1, arguments=' "dentist"}'),
            tc(0, arguments="{}"),
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            "[DONE]",
        ]
        return Reply(chunks=sse(*events))

    async def body(server: FakeServer, backend: LMStudioBackend) -> None:
        deltas = await collect(backend.chat(USER, max_tokens=64, deadline=deadline()))
        assert all(d.tool_calls == () for d in deltas[:-1])  # never a partial call
        final = deltas[-1]
        assert final.finish_reason is FinishReason.TOOL_CALLS
        assert final.tool_calls == (
            ToolCall("call_a", "get_time", "{}"),
            ToolCall("call_b", "search_memory", '{"query": "dentist"}'),
        )

    run(with_server(lmstudio(fragments), body))


def test_chat_sends_tools_schema_and_message_roles_in_wire_format() -> None:
    schema = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}
    messages = [
        Message(Role.SYSTEM, "sys"),
        Message(Role.USER, "u"),
        Message(Role.ASSISTANT, "", tool_calls=(ToolCall("c1", "get_time", "{}"),)),
        Message(Role.TOOL, "now: 10:00", tool_call_id="c1"),
    ]

    async def body(server: FakeServer, backend: LMStudioBackend) -> None:
        await collect(
            backend.chat(
                messages,
                max_tokens=8,
                tools=[ToolSpec("get_time", "the time", {"type": "object", "properties": {}})],
                response_schema=schema,
                deadline=deadline(),
            )
        )
        sent = server.requests[-1].json()
        assert sent["messages"][2] == {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "get_time", "arguments": "{}"}}],
        }
        assert sent["messages"][3] == {"role": "tool", "content": "now: 10:00", "tool_call_id": "c1"}
        assert sent["tools"][0]["function"]["name"] == "get_time"
        assert sent["response_format"]["json_schema"]["schema"] == schema and sent["temperature"] == 0

    run(with_server(lmstudio(lambda r: text_stream("{}")), body))


# --- chat: errors and limits -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "body", "exc"),
    [
        (401, {"error": {"message": "bad"}}, AuthenticationFailed),
        (403, {}, AuthenticationFailed),
        (404, {"error": {"message": "model 'x' not found"}}, ModelNotLoaded),
        (400, {"error": {"message": "Failed to load model"}}, ModelNotLoaded),
        (400, {"error": {"message": "context length exceeded"}}, InvalidRequest),
        (422, {"error": {"message": "bad schema"}}, InvalidRequest),
        (500, {"error": {"message": "oops"}}, BackendUnreachable),
        (503, {}, BackendUnreachable),
    ],
)
def test_chat_maps_http_errors_to_contract_exceptions(status: int, body: dict[str, Any], exc: type[Exception]) -> None:
    async def scenario(server: FakeServer, backend: LMStudioBackend) -> None:
        with pytest.raises(exc):
            await collect(backend.chat(USER, max_tokens=8, deadline=deadline()))

    run(with_server(lmstudio(lambda r: Reply.of_json(body, status)), scenario))


def test_error_messages_never_contain_server_text_prompts_or_the_token() -> None:
    secret_prompt = "my private diary entry"  # noqa: S105

    async def scenario(server: FakeServer, backend: LMStudioBackend) -> None:
        with pytest.raises(InvalidRequest) as info:
            await collect(backend.chat([Message(Role.USER, secret_prompt)], max_tokens=8, deadline=deadline()))
        text = repr(info.value) + str(info.value)
        assert secret_prompt not in text and TOKEN not in text

    run(with_server(lmstudio(lambda r: Reply.of_json({"error": {"message": r.body.decode()}}, 400)), scenario))


def test_chat_connection_refused_is_unreachable() -> None:
    backend = LMStudioBackend(f"http://127.0.0.1:{closed_port()}", token=TOKEN, model=MODEL)
    with pytest.raises(BackendUnreachable):
        run(collect(backend.chat(USER, max_tokens=8, deadline=deadline())))


def test_chat_stream_cut_off_mid_response_is_unreachable_not_a_partial_answer() -> None:
    events = sse({"choices": [{"index": 0, "delta": {"content": "par"}}]})

    async def scenario(server: FakeServer, backend: LMStudioBackend) -> None:
        got: list[Delta] = []
        with pytest.raises(BackendUnreachable):
            async for d in backend.chat(USER, max_tokens=8, deadline=deadline()):
                got.append(d)
        assert all(d.finish_reason is None for d in got)  # no final delta, so no fake "complete" answer

    run(with_server(lmstudio(lambda r: Reply(chunks=events, truncate=True)), scenario))


def test_chat_deadline_applies_to_a_stalled_stream() -> None:
    async def scenario(server: FakeServer, backend: LMStudioBackend) -> None:
        started = time.monotonic()
        with pytest.raises(DeadlineExceeded):
            await collect(backend.chat(USER, max_tokens=8, deadline=deadline(0.4)))
        assert time.monotonic() - started < 3

    run(with_server(lmstudio(lambda r: Reply(stall=True)), scenario))


def test_chat_malformed_event_and_in_stream_error() -> None:
    async def scenario(server: FakeServer, backend: LMStudioBackend) -> None:
        with pytest.raises(BackendUnreachable):
            await collect(backend.chat(USER, max_tokens=8, deadline=deadline()))

    run(with_server(lmstudio(lambda r: Reply(chunks=sse("{not json"))), scenario))

    async def errored(server: FakeServer, backend: LMStudioBackend) -> None:
        with pytest.raises(InvalidRequest):
            await collect(backend.chat(USER, max_tokens=8, deadline=deadline()))

    run(with_server(lmstudio(lambda r: Reply(chunks=sse({"error": {"message": "x"}}))), errored))


def test_chat_caps_tool_call_count_and_argument_size() -> None:
    def many(req: Request) -> Reply:
        calls = [{"index": i, "id": f"c{i}", "function": {"name": "t", "arguments": "{}"}} for i in range(40)]
        return Reply(chunks=sse({"choices": [{"index": 0, "delta": {"tool_calls": calls}}]}))

    def huge(req: Request) -> Reply:
        call = {"index": 0, "id": "c", "function": {"name": "t", "arguments": "x" * 70_000}}
        return Reply(chunks=sse({"choices": [{"index": 0, "delta": {"tool_calls": [call]}}]}))

    async def scenario(server: FakeServer, backend: LMStudioBackend) -> None:
        with pytest.raises(InvalidRequest):
            await collect(backend.chat(USER, max_tokens=8, deadline=deadline()))

    run(with_server(lmstudio(many), scenario))
    run(with_server(lmstudio(huge), scenario))


def test_chat_handles_a_server_that_ends_with_finish_but_no_done_marker() -> None:
    events = sse(
        {"choices": [{"index": 0, "delta": {"content": "ok"}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    )

    async def scenario(server: FakeServer, backend: LMStudioBackend) -> None:
        deltas = await collect(backend.chat(USER, max_tokens=8, deadline=deadline()))
        assert deltas[0].text == "ok" and deltas[-1].finish_reason is FinishReason.STOP

    run(with_server(lmstudio(lambda r: Reply(chunks=events)), scenario))


def test_chat_works_over_a_content_length_body_too() -> None:
    body = b"".join(sse({"choices": [{"index": 0, "delta": {"content": "hi"}}]}, "[DONE]"))

    async def scenario(server: FakeServer, backend: LMStudioBackend) -> None:
        deltas = await collect(backend.chat(USER, max_tokens=8, deadline=deadline()))
        assert deltas[0].text == "hi"

    run(with_server(lmstudio(lambda r: Reply(body=body)), scenario))


# --- credentials and configuration ----------------------------------------------------------------------------------


def test_token_comes_from_a_file_and_never_shows_in_repr(tmp_path: Path) -> None:
    f = tmp_path / "lmstudio-api-token"
    f.write_text(TOKEN + "\n", encoding="utf-8")
    backend = LMStudioBackend.from_token_file("http://127.0.0.1:1234", f)
    assert TOKEN not in repr(backend) and backend.id() == "lmstudio@127.0.0.1"
    assert read_token_file(f) == TOKEN


@pytest.mark.parametrize("content", ["", "  \n", "two words", "a\nb"])
def test_bad_token_files_are_rejected_without_echoing_content(tmp_path: Path, content: str) -> None:
    f = tmp_path / "t"
    f.write_text(content, encoding="utf-8")
    with pytest.raises(CredentialError) as info:
        read_token_file(f)
    assert content.strip() == "" or content.strip() not in str(info.value)
    with pytest.raises(CredentialError):
        read_token_file(tmp_path / "missing")


def test_default_token_file_uses_the_systemd_credentials_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    assert default_token_file() is None
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", "/run/credentials/atlas-core.service")
    assert default_token_file() == Path("/run/credentials/atlas-core.service/lmstudio-api-token")


@pytest.mark.parametrize(
    "url", ["https://10.77.0.2:1234", "10.77.0.2:1234", "http://u:p@10.77.0.2:1234", "http://h:1/v1"]
)
def test_only_plain_http_host_port_urls_are_accepted(url: str) -> None:
    with pytest.raises(ValueError):
        LMStudioBackend(url)


def test_capabilities_and_default_url() -> None:
    b = LMStudioBackend()
    assert b.id() == "lmstudio@10.77.0.2"
    caps = b.capabilities()
    assert caps.tools and caps.json_schema and caps.streaming and caps.max_ctx == 16384
    assert json.dumps(caps.max_ctx)
