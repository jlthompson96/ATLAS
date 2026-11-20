"""The Pi fallback router's deterministic grammar (PLAN.md §5.4, §5.6, Phase 1).

A handful of things ATLAS can answer on the Pi without the LLM: the time, the date, whether the
desktop is awake, and waking or sleeping it. They run first for every request (so they are instant
and work with the host asleep, offline or busy); everything else goes to the LLM, or is queued when
the core is unreachable.

The grammar is strict on purpose. An utterance matches only if the *whole* sentence fits a pattern
(after lower-casing, stripping punctuation and polite filler), so "what time is the meeting with
Dana" is not a time request. It sees only text the user typed or spoke, never tool output or model
output, so nothing here can be steered by untrusted content. The fallback *LLM* for the long tail
(B7) is a separate, later piece (``atlas-fallback``); this module is the part that needs no model.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from datetime import datetime
from enum import StrEnum
from typing import Final

from atlas.power.interface import (
    PowerController,
    PowerState,
    SleepAccepted,
    SleepReason,
    SleepRefused,
    WakeAttempt,
    WakeReason,
    WakeRefusal,
    WakeRefused,
)


class Intent(StrEnum):
    TIME = "time"
    DATE = "date"
    HOST_STATUS = "host_status"
    WAKE_HOST = "wake_host"
    SLEEP_HOST = "sleep_host"
    HELP = "help"


MAX_WORDS: Final = 12
_HOST = r"(?:the |my )?(?:desktop|core|host|pc|computer|llm|server|gaming pc)"
_PATTERNS: Final[tuple[tuple[Intent, re.Pattern[str]], ...]] = tuple(
    (intent, re.compile(rf"^(?:{pattern})$"))
    for intent, pattern in (
        (
            Intent.TIME,
            r"(?:what(?:s| is)? )?(?:the )?(?:current )?time(?: is it)?(?: now| right now)?"
            r"|what time is it(?: now| right now)?|tell me the time|do you have the time|got the time",
        ),
        (
            Intent.DATE,
            r"(?:what(?:s| is)? )?(?:the |todays? )?(?:todays? )?date(?: today)?"
            r"|what day is it(?: today)?|what day is today|what day of the week is it|what is today",
        ),
        (
            Intent.HOST_STATUS,
            rf"is {_HOST} (?:awake|up|online|asleep|on|running|ready|available|alive)"
            rf"|{_HOST} status|status of {_HOST}|(?:host |core |system )?status",
        ),
        (
            Intent.WAKE_HOST,
            rf"wake(?: up)? {_HOST}(?: up)?|turn on {_HOST}|start {_HOST}",
        ),
        (
            Intent.SLEEP_HOST,
            rf"(?:put|send) {_HOST} (?:to )?sleep|sleep {_HOST}|{_HOST} (?:go to )?sleep|turn off {_HOST}",
        ),
        (Intent.HELP, r"help|what can you do(?: offline| without the core)?|what can you do right now"),
    )
)
_LEAD: Final = re.compile(r"^(?:(?:hey|ok|okay) )?(?:atlas )?(?:(?:please|could you|can you|would you) )*")
_TAIL: Final = re.compile(r"(?: (?:please|for me|atlas|now please))+$")
_PUNCT: Final = re.compile(r"[^a-z0-9' ]+")


def normalize(text: str) -> str:
    out = _PUNCT.sub(" ", text.lower().replace("’", "'"))
    out = " ".join(out.split()).replace("'", "")
    out = _LEAD.sub("", out)
    return _TAIL.sub("", out).strip()


def parse_intent(text: str) -> Intent | None:
    """The intent a whole utterance expresses, or ``None`` (send it to the LLM)."""
    cleaned = normalize(text)
    if not cleaned or len(cleaned.split()) > MAX_WORDS:
        return None
    for intent, pattern in _PATTERNS:
        if pattern.match(cleaned):
            return intent
    return None


HELP_TEXT: Final = (
    "Without my core I can tell the time and date, say whether the desktop is awake, "
    "and wake it or put it to sleep. Anything else I'll save for when it's back."
)
_STATE_LINES: Final = {
    PowerState.UP: "The core is awake.",
    PowerState.WAKING: "The core is waking up.",
    PowerState.ASLEEP: "The core is asleep.",
    PowerState.UNKNOWN: "I can't reach the core and I don't know why. It may be asleep or off.",
}
_WAKE_REFUSALS: Final = {
    WakeRefusal.ALREADY_UP: "The core is already awake.",
    WakeRefusal.RATE_LIMITED: "I've woken it too often this hour.",
    WakeRefusal.BACKOFF: "I tried recently and it didn't wake. I'll hold off for a bit.",
    WakeRefusal.OUTSIDE_ACTIVE_HOURS: "That's outside my active hours.",
    WakeRefusal.OUTSIDE_WINDOW: "That's outside the research window.",
}


class LocalResponder:
    """Executes a grammar intent. ``power`` may be ``None`` (fake backend): host intents then say so."""

    def __init__(
        self,
        now: Callable[[], datetime],
        power: PowerController | None = None,
        *,
        wake: Callable[[], Awaitable[str]] | None = None,
        sleep: Callable[[], Awaitable[SleepAccepted | SleepRefused]] | None = None,
    ) -> None:
        self._now = now
        self._power = power
        self._wake = wake
        self._sleep = sleep

    async def respond(self, intent: Intent) -> str:
        if intent is Intent.TIME:
            return self._now().strftime("It's %I:%M %p.").replace("It's 0", "It's ")
        if intent is Intent.DATE:
            return self._now().strftime("Today is %A, %B %d, %Y.").replace(" 0", " ")
        if intent is Intent.HELP:
            return HELP_TEXT
        if self._power is None:
            return "There's no desktop connected in this mode."
        if intent is Intent.HOST_STATUS:
            return _STATE_LINES[await self._power.state()]
        if intent is Intent.WAKE_HOST:
            if self._wake is not None:
                return await self._wake()
            result = await self._power.wake(WakeReason.MANUAL)
            if isinstance(result, WakeRefused):
                return _WAKE_REFUSALS.get(result.reason, "I can't wake it right now.")
            return "Waking my core." if isinstance(result, WakeAttempt) else "I can't wake it right now."
        answer = await (self._sleep() if self._sleep is not None else self._power.request_sleep(SleepReason.IDLE))
        if isinstance(answer, SleepAccepted):
            return "Putting the desktop to sleep."
        if isinstance(answer, SleepRefused):
            return "Not now: " + ", ".join(r.replace("_", " ") for r in answer.reasons) + "."
        return "I can't do that right now."
