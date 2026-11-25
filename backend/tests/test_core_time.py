"""get_time: the time as a person reads it, in the Pi's zone, plus UTC (a model shown only UTC gives the wrong hour)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from typing import Any

import pytest

from atlas.core.prompt import TrustedText
from atlas.core.tools import GetTime

EST = timezone(timedelta(hours=-5), "EST")
IST = timezone(timedelta(hours=5, minutes=30), "IST")
NST = timezone(timedelta(hours=-3, minutes=-30), "NST")


def reading(when: str, tz: tzinfo | None) -> str:
    ms = int(datetime.fromisoformat(when).timestamp() * 1000)
    none: Any = None  # get_time reads no arguments, taint or context
    out = asyncio.run(GetTime(lambda: ms, tz).run({}, none, none, none))
    (part,) = out.parts()  # one line of ATLAS text, no variables
    assert isinstance(part, TrustedText)
    return part.text


@pytest.mark.parametrize(
    ("when", "tz", "expected"),
    [
        # The case found on 2026-11-25: 14:08 UTC is 9:08 in the morning for the owner.
        ("2026-11-25T14:08:41+00:00", EST,
         "now: 9:08 AM, Wednesday, November 25, 2026 (EST, UTC-05:00); UTC 2026-11-25T14:08:41Z"),
        # Local date differs from the UTC date.
        ("2026-11-26T02:30:00+00:00", EST,
         "now: 9:30 PM, Wednesday, November 25, 2026 (EST, UTC-05:00); UTC 2026-11-26T02:30:00Z"),
        # Midnight and noon on a 12-hour clock.
        ("2026-11-25T05:05:00+00:00", EST,
         "now: 12:05 AM, Wednesday, November 25, 2026 (EST, UTC-05:00); UTC 2026-11-25T05:05:00Z"),
        ("2026-11-25T17:00:00+00:00", EST,
         "now: 12:00 PM, Wednesday, November 25, 2026 (EST, UTC-05:00); UTC 2026-11-25T17:00:00Z"),
        # Half-hour zones, on both sides of UTC.
        ("2026-11-25T14:08:00+00:00", IST,
         "now: 7:38 PM, Wednesday, November 25, 2026 (IST, UTC+05:30); UTC 2026-11-25T14:08:00Z"),
        ("2026-11-25T14:08:00+00:00", NST,
         "now: 10:38 AM, Wednesday, November 25, 2026 (NST, UTC-03:30); UTC 2026-11-25T14:08:00Z"),
        ("2026-11-25T14:08:00+00:00", UTC,
         "now: 2:08 PM, Wednesday, November 25, 2026 (UTC, UTC+00:00); UTC 2026-11-25T14:08:00Z"),
    ],
)  # fmt: skip
def test_get_time_reads_like_a_clock(when: str, tz: tzinfo, expected: str) -> None:
    assert reading(when, tz) == expected


def test_get_time_uses_the_systems_zone_by_default() -> None:
    when = "2026-11-25T14:08:41+00:00"
    local = datetime.fromisoformat(when).astimezone()
    out = reading(when, None)
    assert f"({local.tzname()}," in out and out.endswith("; UTC 2026-11-25T14:08:41Z")
