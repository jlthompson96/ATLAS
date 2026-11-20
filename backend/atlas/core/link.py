"""Wiring for the real LLM link: LM Studio + atlas-hostd + Wake-on-LAN (PLAN.md Phase 1).

``build_link`` turns CLI options into a ``Link``. Two modes:

* **full** (hostd configured): ``HostPowerController`` + ``HostGate`` run the wake flow, policy and
  idle-sleep; the HUD-facing events are emitted as events.v1 ``state.changed`` / ``alert`` payloads.
* **direct** (``--no-hostd``): LM Studio only, no wake and no sleep. For the first LAN test before
  hostd and WireGuard exist (docs/runbooks/llm-link-dev.md).
"""

from __future__ import annotations

import argparse
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from atlas.core.auditing import AuditSink
from atlas.core.control import Readiness
from atlas.core.fallback import LocalResponder, parse_intent
from atlas.core.pending import EventSink, ListEventSink
from atlas.llm.interface import LLMBackend, LLMError, Ready
from atlas.llm.lmstudio import DEFAULT_BASE_URL, CredentialError, LMStudioBackend, default_token_file
from atlas.power.controller import Clock, HostPowerController, PowerConfig, SystemClock
from atlas.power.flow import (
    LINE_STILL_WAKING,
    LINE_WAKING,
    HostGate,
    HostReady,
    HostUnavailable,
    RequestQueue,
    emit_state,
)
from atlas.power.hostd import DEFAULT_HOSTD_URL, HostdClient
from atlas.power.interface import SleepAccepted, SleepReason, SleepRefused, WakeReason
from atlas.power.wol import DEFAULT_BROADCAST, DEFAULT_PORT, UdpWolSender

SPOKEN_PROGRESS_S = (15, 35)  # PLAN.md 5.4: "still waking" lines


def add_link_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--backend", choices=("fake", "lmstudio"), default="fake", help="LLM backend (default: fake)")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="LM Studio URL, http://host:port")
    parser.add_argument(
        "--token-file", default=None, help="file holding the LM Studio API token (default: $CREDENTIALS_DIRECTORY)"
    )
    parser.add_argument("--model", default=None, help="model id (default: the model LM Studio has loaded)")
    parser.add_argument("--hostd-url", default=DEFAULT_HOSTD_URL, help="atlas-hostd URL, http://host:port")
    parser.add_argument("--no-hostd", action="store_true", help="LM Studio only: no wake, no sleep, no hostd")
    parser.add_argument("--mac", default=None, help="desktop MAC address for Wake-on-LAN, aa:bb:cc:dd:ee:ff")
    parser.add_argument("--wol-broadcast", default=DEFAULT_BROADCAST, help="Wake-on-LAN broadcast address")
    parser.add_argument("--wol-port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--model-vram-mb", type=int, default=None, help="VRAM the model needs, for the HOST: BUSY check"
    )
    parser.add_argument("--idle-sleep", action="store_true", help="put the desktop to sleep after 20 idle minutes")


class TerminalEvents(ListEventSink):
    """Collects events and prints the parts a person at a terminal should see."""

    def __init__(self, out: TextIO, clock: Clock) -> None:
        super().__init__()
        self._out = out
        self._clock = clock
        self._said: set[int] = set()

    async def emit(self, type_: str, payload: Mapping[str, Any]) -> None:
        await super().emit(type_, payload)
        if type_ == "alert":
            print(f"[alert {payload['level']} {payload['code']}] {payload['message']}", file=self._out)
        elif type_ == "state.changed" and payload["state"] == "waking_host":
            wake = payload["wake"]
            if not self._said:
                self._said.add(0)
                print(LINE_WAKING, file=self._out)
            for mark in SPOKEN_PROGRESS_S:
                if wake["elapsed_ms"] >= mark * 1000 and mark not in self._said:
                    self._said.add(mark)
                    print(LINE_STILL_WAKING, file=self._out)
            print(
                f"[waking host: stage {wake['stage']}, {wake['elapsed_ms'] // 1000}s of {wake['deadline_ms'] // 1000}s]",
                file=self._out,
            )
        elif type_ == "state.changed" and payload["state"] in ("thinking", "idle", "llm_unreachable"):
            self._said.clear()


@dataclass
class Link:
    backend: LMStudioBackend
    model: str
    events: EventSink
    clock: Clock
    controller: HostPowerController | None = None
    gate: HostGate | None = None

    async def ensure_ready(self, text: str | None, reason: WakeReason = WakeReason.INTERACTIVE) -> str | None:
        """``None`` when the model is ready, else the line to show the user (and the request is queued)."""
        if self.gate is not None:
            result = await self.gate.ensure_ready(reason, request_text=text)
            return result.line if isinstance(result, HostUnavailable) else None
        outcome = await self.backend.ensure_ready(self.model, time.monotonic() + 30.0)
        if isinstance(outcome, Ready):
            return None
        return f"The model isn't ready ({outcome.stage.value}: {outcome.reason})."

    async def wake_command(self) -> str:
        """The spoken/typed "wake the core": runs the full flow so the progress shows."""
        if self.gate is None:
            return "No hostd is configured, so I can't wake the desktop."
        result = await self.gate.ensure_ready(WakeReason.MANUAL)
        if isinstance(result, HostReady):
            if result.woke:
                await emit_state(self.events, "idle")  # the flow left the orb in "thinking"
            return "The core is awake." if result.woke else "The core is already awake."
        return result.line

    async def sleep_command(self) -> SleepAccepted | SleepRefused:
        if self.controller is None:
            return SleepRefused(("no_hostd",))
        return await self.controller.request_sleep(SleepReason.IDLE, user_requested=True)


async def resolve_model(backend: LLMBackend, requested: str | None) -> str | None:
    if requested:
        return requested
    try:
        models = await backend.list_models(time.monotonic() + 10.0)
    except LLMError:
        return None
    loaded = [m.id for m in models if m.loaded and m.extra.get("type", "llm") in ("llm", "vlm")]
    return loaded[0] if loaded else None


async def build_link(
    args: argparse.Namespace, audit: AuditSink, out: TextIO, *, events: EventSink | None = None
) -> Link:
    """Raises ``SystemExit`` with a plain message for configuration mistakes.

    ``events``: where wake progress and alerts go. Default: printed to ``out`` (the CLI). atlas-ui's dev
    runner passes the core's ``EventHub`` so the HUD shows them instead.
    """
    token_file = Path(args.token_file) if args.token_file else default_token_file()
    try:
        backend = LMStudioBackend.from_token_file(args.base_url, token_file)
    except (CredentialError, ValueError) as exc:
        raise SystemExit(f"error: {exc}") from None
    model = await resolve_model(backend, args.model)
    if model is None:
        raise SystemExit("error: no model given and none is loaded; pass --model <id>")
    backend = LMStudioBackend.from_token_file(args.base_url, token_file, model=model)
    clock = SystemClock()
    link = Link(backend, model, events if events is not None else TerminalEvents(out, clock), clock)
    if args.no_hostd:
        return link
    try:
        hostd = HostdClient(args.hostd_url)
        sender = UdpWolSender(args.mac, broadcast=args.wol_broadcast, port=args.wol_port) if args.mac else None
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from None
    queue = RequestQueue(clock)
    link.controller = HostPowerController(
        hostd, sender, audit, clock=clock, config=PowerConfig(), queue_busy=lambda: False
    )
    link.gate = HostGate(
        link.controller,
        backend,
        hostd,
        model=model,
        events=link.events,
        clock=clock,
        queue=queue,
        model_vram_mb=args.model_vram_mb,
    )
    return link


class LinkTextHooks:
    """What the control socket runs around a typed request when the real LLM link is in use.

    The same order as the CLI: the local grammar first (time, date, desktop wake/sleep/status never reach the
    model), then the wake gate, then the orchestrator.
    """

    def __init__(self, link: Link) -> None:
        self._link = link
        self._responder = LocalResponder(
            link.clock.now, link.controller, wake=link.wake_command, sleep=link.sleep_command
        )

    async def local_answer(self, text: str) -> str | None:
        intent = parse_intent(text)
        return None if intent is None else await self._responder.respond(intent)

    async def ensure_ready(self, text: str) -> Readiness:
        line = await self._link.ensure_ready(text)
        if line is not None:
            return Readiness(line)
        if self._link.controller is not None:
            self._link.controller.note_llm_request()
        saved = tuple(s.text for s in self._link.gate.queue.drain()) if self._link.gate is not None else ()
        return Readiness(None, saved)
