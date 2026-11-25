"""Replies to senders with a display name (B5 2026-11-25 finding): ``$FROM_n`` holds the header value.

``mailbox_address`` takes the address out of ``Name <addr>`` strictly: exactly one ``<...>``, at the end.
The display name is attacker-controlled and dropped; the approval card shows the bare address, and the
proposal stays tainted with the usual recipient warnings (RT-14).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Coroutine
from typing import Any

import pytest

from atlas.core.fake_llm import ScriptedBackend, call_turn, text_turn
from atlas.policy.payloads import mailbox_address
from tests.core_support import Harness


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


@pytest.mark.parametrize(
    ("raw", "address"),
    [
        ("dana@example.org", "dana@example.org"),
        ("  dana@example.org ", "dana@example.org"),
        ("Dana <dana@example.org>", "dana@example.org"),
        ('"Dana Q." <dana.q@example.org>', "dana.q@example.org"),
        ("<dana@example.org>", "dana@example.org"),
        # A lookalike display name is dropped: the bare address is what the approval card shows.
        ("support@bank.example.com <mallory@example.net>", "mallory@example.net"),
        ("Dana <dana@example.org>, Eve <eve@example.net>", None),
        ("Dana <dana@example.org> (work)", None),
        ("Dana <dana@example.org", None),
        ("Dana <not an address>", None),
        ("Dana <<dana@example.org>>", None),
        ("Team: a@example.com, b@example.com;", None),
        ("Dana <dana@[192.0.2.1]>", None),
        ("", None),
    ],
)
def test_mailbox_address(raw: str, address: str | None) -> None:
    assert mailbox_address(raw) == address


def _reply(sender: str, to: list[str]) -> Harness:
    turns = [
        call_turn(("read_index", {"source": "email"})),
        call_turn(
            (
                "propose_action",
                {
                    "kind": "email.send",
                    "summary": "Reply: 2pm works",
                    "to": to,
                    "subject": "$SUBJECT_1",
                    "body": ["2pm works."],
                    "in_reply_to": "$EMAIL_1",
                },
            )
        ),
        text_turn("Proposed."),
    ]
    h = Harness(backend=ScriptedBackend(turns))
    h.add_item("Saturday at 2pm?", sender=sender, subject="Saturday")
    run(h.run("Reply that 2pm works."))
    return h


def test_reply_to_a_display_name_sender_proposes_the_bare_address_flagged() -> None:
    h = _reply("Dana Q. <dana.q@example.net>", ["$FROM_1"])
    (action,) = h.runtime.pending.all()
    assert json.loads(action.payload_canonical)["to"] == ["dana.q@example.net"]
    assert action.provenance.tainted
    assert {w.value for w in action.warnings} >= {"not_on_allowlist", "new_recipient"}
    (event,) = h.pending_events()
    assert event["provenance"]["tainted"] is True
    h.assert_no_unapproved_action()


def test_reply_to_an_allowlisted_display_name_sender_has_no_recipient_warning() -> None:
    h = _reply("Dana <dana@example.org>", ["$FROM_1"])
    (action,) = h.runtime.pending.all()
    assert json.loads(action.payload_canonical)["to"] == ["dana@example.org"]
    assert not {w.value for w in action.warnings} & {"not_on_allowlist", "new_recipient"}


@pytest.mark.parametrize("sender", ["Boss <boss@example.org>, Mallory <m@example.net>", "Dana <dana@example.org> x"])
def test_ambiguous_sender_headers_are_refused_with_a_fixable_detail(sender: str) -> None:
    h = _reply(sender, ["$FROM_1"])
    assert h.runtime.pending.all() == []
    refusal = h.backend.calls[-1].messages[-1].content
    assert refusal == "refused: INVALID_RECIPIENT (to: not one email address)"
    assert "m@example.net" not in refusal and "boss" not in refusal


def test_literal_recipients_stay_bare_addresses() -> None:
    # (Typing the sender's own header text instead is a taint leak: the whole request fails closed.)
    h = _reply("someone@example.net", ["Dana <dana@example.org>"])
    assert h.runtime.pending.all() == []
    assert h.backend.calls[-1].messages[-1].content.startswith("refused: INVALID_ARGS")


def test_contact_names_still_match_display_name_senders_and_not_ambiguous_ones() -> None:
    h = Harness(backend=ScriptedBackend([call_turn(("read_index", {"source": "email"})), text_turn("ok")]))
    h.runtime.index._contacts = {"dana@example.org": "Dana"}  # same as passing contacts= to build_runtime
    h.add_item("one", rid="gmail:msg/c1", sender="Dana Q. <DANA@example.org>")
    h.add_item("two", rid="gmail:msg/c2", sender="Dana <dana@example.org>, Eve <eve@example.net>")
    run(h.run("List my email."))
    listing = h.backend.calls[-1].messages[-1].content
    assert listing.count("contact 'Dana' (claimed)") == 1
    assert "$FROM_1" in listing  # the ambiguous one stays a tainted reference
