"""Pure parsers for Windows tool output and NVML bitmasks. Platform-independent, so they are tested on the Mac.

The sample texts the tests use are modelled on documented output formats; they have NOT been verified against
the real desktop (see hostd/README.md, Windows test checklist).
"""

from __future__ import annotations

import csv
import io
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime

from atlas_hostd.models import ResumeInfo, SleepState, WakeSource


def parse_powercfg_available(text: str) -> tuple[SleepState, ...]:
    """Sleep states from the *available* section of ``powercfg /a``.

    Matches on the non-localised tokens ``(S3)`` and ``S0 Low Power Idle``; ``Hibernate`` is matched by its
    English name, so a localised Windows may under-report hibernate (never S3 or Modern Standby).
    """
    found: set[SleepState] = set()
    for line in text.splitlines():
        lowered = line.strip().lower()
        if "not available" in lowered or "nicht verf" in lowered:
            break  # everything after this heading is unavailable states
        if "(s3)" in lowered:
            found.add(SleepState.S3)
        elif "s0 low power idle" in lowered:
            found.add(SleepState.MODERN_STANDBY)
        elif lowered == "hibernate":
            found.add(SleepState.HIBERNATE)
    order = (SleepState.S3, SleepState.MODERN_STANDBY, SleepState.HIBERNATE)
    return tuple(s for s in order if s in found)


_NETWORK_WORDS = re.compile(r"ethernet|gbe|network|\blan\b|\bnic\b|wi-?fi|wireless|magic packet|wake on lan", re.I)
_BUTTON_WORDS = re.compile(r"power button|sleep button|\bbutton\b|\blid\b", re.I)
_INPUT_WORDS = re.compile(r"keyboard|mouse|\bhid\b|touchpad|input", re.I)
_TIMER_WORDS = re.compile(r"timer|\brtc\b|scheduled|wake timer", re.I)


def classify_wake_source(text: str) -> WakeSource:
    """Generalise Windows' wake-source text. Device names never leave this function."""
    if not text.strip():
        return WakeSource.UNKNOWN
    if _NETWORK_WORDS.search(text):
        return WakeSource.NETWORK
    if _BUTTON_WORDS.search(text):
        return WakeSource.POWER_BUTTON
    if _INPUT_WORDS.search(text):
        return WakeSource.INPUT_DEVICE
    if _TIMER_WORDS.search(text):
        return WakeSource.TIMER
    return WakeSource.UNKNOWN


def _parse_event_time(value: str) -> datetime | None:
    value = value.strip().replace("Z", "+00:00")
    # Event timestamps carry up to 7 fractional digits; datetime accepts at most 6.
    value = re.sub(r"(\.\d{6})\d+", r"\1", value)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_resume_event_xml(xml_text: str) -> ResumeInfo | None:
    """One Microsoft-Windows-Power-Troubleshooter event 1 (``wevtutil qe ... /f:xml``) into a ``ResumeInfo``.

    Uses ``WakeTime`` from the event data when present, else the event's ``TimeCreated``. The wake source is the
    classification of every data value whose name contains ``WakeSource``.
    """
    try:
        root = ET.fromstring(xml_text.strip())  # noqa: S314
    except ET.ParseError:
        return None
    created: datetime | None = None
    data: dict[str, str] = {}
    for elem in root.iter():
        tag = elem.tag.rsplit("}", 1)[-1]
        if tag == "TimeCreated":
            created = _parse_event_time(elem.attrib.get("SystemTime", ""))
        elif tag == "Data":
            data[elem.attrib.get("Name", "")] = (elem.text or "").strip()
    at = _parse_event_time(data.get("WakeTime", "")) or created
    if at is None:
        return None
    source_text = " ".join(v for k, v in data.items() if "wakesource" in k.lower())
    return ResumeInfo(at=at, source=classify_wake_source(source_text))


# NVML clocks-throttle-reason bits (nvml.h). Idle, applications-clocks and display-clock settings are benign.
_THROTTLE_THERMAL = 0x20 | 0x40
_THROTTLE_POWER = 0x4 | 0x80
_THROTTLE_OTHER = 0x8 | 0x10


def classify_throttle(mask: int) -> tuple[str, ...]:
    out: list[str] = []
    if mask & _THROTTLE_THERMAL:
        out.append("thermal")
    if mask & _THROTTLE_POWER:
        out.append("power")
    if mask & _THROTTLE_OTHER:
        out.append("other")
    return tuple(out)


_GPU_PID = re.compile(r"pid_(\d+)_")


def gpu_heavy_pids(typeperf_csv: str, threshold_pct: float) -> dict[int, float]:
    """Per-PID total GPU-engine utilisation from ``typeperf "\\GPU Engine(*)\\Utilization Percentage" -sc 2``.

    Uses the last data row (rate counters need two samples) and returns only PIDs at or above the threshold.
    """
    rows = [row for row in csv.reader(io.StringIO(typeperf_csv)) if row]
    header_idx = next((i for i, row in enumerate(rows) if row and row[0].startswith("(PDH-CSV")), None)
    if header_idx is None:
        return {}
    header = rows[header_idx]
    data_rows = [row for row in rows[header_idx + 1 :] if len(row) == len(header)]
    if not data_rows:
        return {}
    totals: dict[int, float] = {}
    for name, value in zip(header[1:], data_rows[-1][1:], strict=True):
        match = _GPU_PID.search(name)
        if match is None:
            continue
        try:
            util = float(value)
        except ValueError:
            continue
        pid = int(match.group(1))
        totals[pid] = totals.get(pid, 0.0) + util
    return {pid: total for pid, total in totals.items() if total >= threshold_pct}
