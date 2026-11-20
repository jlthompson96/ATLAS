"""Typed HUD requests with a real LLM link: control socket + text hooks (grammar, wake gate, saved requests).

The stub-hook tests pin the control socket's order of operations; the last two run the real ``LinkTextHooks``
against the fake LM Studio from ``tests.net_support``.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from atlas.core.control import EventHub, Readiness, SameUserPeers
from atlas.core.control_client import ControlClient
from atlas.core.link import LinkTextHooks, add_link_arguments, build_link
from atlas.ui_server.dev import CoreStack, build_core
from tests.net_support import TOKEN, FakeServer
from tests.test_cli_lmstudio import lmstudio
from tests.ui_support import InstantExecutor

Json = dict[str, Any]
Events = list[tuple[str, Json]]


class StubHooks:
    def __init__(self, *, local: str | None = None, ready: Readiness | None = None, hub: EventHub | None = None):
        self.local = local
        self.ready = ready or Readiness(None)
        self.hub = hub
        self.calls: list[str] = []

    async def local_answer(self, text: str) -> str | None:
        self.calls.append(f"local:{text}")
        return self.local

    async def ensure_ready(self, text: str) -> Readiness:
        self.calls.append(f"ready:{text}")
        if self.ready.line is not None and self.hub is not None:  # what HostGate does when the host stays down
            await self.hub.emit("state.changed", {"state": "llm_unreachable", "code": "LLM_TIMEOUT"})
        return self.ready


def run_text(
    hooks_for: Callable[[EventHub], Any], text: str, check: Callable[[CoreStack, Events], Awaitable[None]]
) -> None:
    async def go() -> None:
        base = Path(tempfile.mkdtemp(prefix="atlas-core-", dir="/tmp"))
        hub = EventHub()
        core = build_core(peers=SameUserPeers(), executor=InstantExecutor(), hub=hub, text_hooks=hooks_for(hub))
        events: Events = []
        hub.subscribe(lambda t, p: events.append((t, p)))
        sock = base / "core.sock"
        await core.control.start(sock)
        client = ControlClient(sock, lambda t, p: None)
        await client.connect()
        try:
            assert (await client.call("text.input", device_id="hud-1", text=text))["ok"] is True
            await core.control.drain()
            await check(core, events)
        finally:
            await client.close()
            await core.control.stop()
            shutil.rmtree(base, ignore_errors=True)

    asyncio.run(go())


def finals(events: Events) -> list[tuple[str, str, str]]:
    return [(p["utterance_id"], p["role"], p["text"]) for t, p in events if t == "transcript.final"]


def states(events: Events) -> list[str]:
    return [p["state"] for t, p in events if t == "state.changed"]


def test_a_grammar_answer_skips_the_model_and_the_thinking_state() -> None:
    hooks = StubHooks(local="It's 9:41 AM.")

    async def check(core: CoreStack, events: Events) -> None:
        assert finals(events) == [("u1", "user", "what time is it"), ("a1", "assistant", "It's 9:41 AM.")]
        assert states(events) == []
        assert hooks.calls == ["local:what time is it"]  # never asked to wake, never reached the orchestrator

    run_text(lambda hub: hooks, "what time is it", check)


def test_an_unreachable_host_answers_with_the_gate_line_and_keeps_its_state() -> None:
    line = "I can't reach my core right now. I've saved your request."
    holder: dict[str, StubHooks] = {}

    def make(hub: EventHub) -> StubHooks:
        holder["h"] = StubHooks(ready=Readiness(line), hub=hub)
        return holder["h"]

    async def check(core: CoreStack, events: Events) -> None:
        assert finals(events)[-1] == ("a1", "assistant", line)
        assert states(events) == ["thinking", "llm_unreachable"]  # not reset to idle behind the gate's back
        assert core.hub.model.state["state"] == "llm_unreachable"
        assert holder["h"].calls == ["local:hello", "ready:hello"]

    run_text(make, "hello", check)


def test_saved_requests_are_answered_first_then_the_new_one_and_the_orb_rests_idle() -> None:
    hooks = StubHooks(ready=Readiness(None, saved=("what time is it",)))

    async def check(core: CoreStack, events: Events) -> None:
        ids = [(uid, role) for uid, role, _ in finals(events)]
        assert ids == [("u1", "user"), ("a1q0", "assistant"), ("a1", "assistant")]
        assert states(events)[0] == "thinking" and states(events)[-1] == "idle"

    run_text(lambda hub: hooks, "hello", check)


def test_without_hooks_the_scripted_path_is_unchanged() -> None:
    async def check(core: CoreStack, events: Events) -> None:
        assert [(u, r) for u, r, _ in finals(events)] == [("u1", "user"), ("a1", "assistant")]
        assert states(events) == ["thinking", "idle"]

    run_text(lambda hub: None, "what time is it", check)


# --- the real LinkTextHooks against a fake LM Studio ----------------------------------------------------


def lmstudio_args(url: str, token_file: Path) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    add_link_arguments(parser)
    return parser.parse_args(
        ["--backend", "lmstudio", "--base-url", url, "--token-file", str(token_file), "--no-hostd"]
    )


def with_lmstudio(tmp_path: Path, text: str, check: Callable[[FakeServer, Events], None]) -> None:
    token_file = tmp_path / "lmstudio-api-token"
    token_file.write_text(TOKEN + "\n", encoding="utf-8")

    async def go() -> None:
        server = FakeServer(lmstudio)
        await server.start()
        base = Path(tempfile.mkdtemp(prefix="atlas-core-", dir="/tmp"))
        try:
            hub = EventHub()
            core0 = build_core(peers=SameUserPeers(), executor=InstantExecutor(), hub=hub)
            link = await build_link(lmstudio_args(server.url, token_file), core0.audit, io.StringIO(), events=hub)
            core = build_core(
                peers=SameUserPeers(),
                executor=InstantExecutor(),
                backend=link.backend,
                hub=hub,
                audit=core0.audit,
                text_hooks=LinkTextHooks(link),
            )
            events: Events = []
            hub.subscribe(lambda t, p: events.append((t, p)))
            sock = base / "core.sock"
            await core.control.start(sock)
            client = ControlClient(sock, lambda t, p: None)
            await client.connect()
            assert (await client.call("text.input", device_id="hud-1", text=text))["ok"] is True
            await core.control.drain()
            await client.close()
            await core.control.stop()
            check(server, events)
        finally:
            await server.close()
            shutil.rmtree(base, ignore_errors=True)

    asyncio.run(go())


def model_chats(server: FakeServer) -> list[Any]:
    return [r for r in server.requests if r.path == "/v1/chat/completions" and r.json().get("max_tokens") != 1]


def test_hud_text_reaches_lm_studio_through_the_orchestrator(tmp_path: Path) -> None:
    def check(server: FakeServer, events: Events) -> None:
        assert finals(events)[-1] == ("a1", "assistant", "Hello from the fake model.")
        assert states(events) == ["thinking", "idle"]
        chats = model_chats(server)
        assert len(chats) == 1 and chats[0].json()["messages"][0]["role"] == "system"
        assert all(TOKEN not in str(p) for _, p in events)

    with_lmstudio(tmp_path, "hello", check)


def test_hud_time_question_is_answered_locally_and_never_reaches_lm_studio(tmp_path: Path) -> None:
    def check(server: FakeServer, events: Events) -> None:
        assert finals(events)[-1][2].startswith("It's ")
        assert model_chats(server) == []

    with_lmstudio(tmp_path, "what time is it", check)
