"""Pure parsers for Windows tool output. The samples are modelled on documented formats, NOT captured from the
real desktop; the Windows checklist in hostd/README.md has the owner confirm them against real output."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from atlas_hostd.models import SleepState, WakeSource
from atlas_hostd.platform.parsers import (
    classify_throttle,
    classify_wake_source,
    gpu_heavy_pids,
    parse_powercfg_available,
    parse_resume_event_xml,
)

POWERCFG_S3 = """The following sleep states are available on this system:
    Standby (S3)
    Hibernate
    Hybrid Sleep

The following sleep states are not available on this system:
    Standby (S1)
        The system firmware does not support this standby state.

    Standby (S0 Low Power Idle)
        The system firmware does not support this standby state.
"""

POWERCFG_MODERN = """The following sleep states are available on this system:
    Standby (S0 Low Power Idle) Network Connected
    Hibernate
    Fast Startup

The following sleep states are not available on this system:
    Standby (S1)
        The system firmware does not support this standby state.
    Standby (S3)
        The system firmware does not support this standby state.
"""


def test_powercfg_s3_machine_ignores_unavailable_section() -> None:
    assert parse_powercfg_available(POWERCFG_S3) == (SleepState.S3, SleepState.HIBERNATE)


def test_powercfg_modern_standby_machine() -> None:
    assert parse_powercfg_available(POWERCFG_MODERN) == (SleepState.MODERN_STANDBY, SleepState.HIBERNATE)


def test_powercfg_garbage_means_no_states() -> None:
    assert parse_powercfg_available("") == ()
    assert parse_powercfg_available("access denied") == ()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Wake Source: Device - Example Gigabit Ethernet Controller", WakeSource.NETWORK),
        ("Wake Source: Device - Realtek PCIe GbE Family Controller", WakeSource.NETWORK),
        ("Wake Source: Power Button", WakeSource.POWER_BUTTON),
        ("Wake Source: Device - HID Keyboard Device", WakeSource.INPUT_DEVICE),
        ("Wake Source: Wake timer", WakeSource.TIMER),
        ("Wake Source: Unknown", WakeSource.UNKNOWN),
        ("", WakeSource.UNKNOWN),
    ],
)
def test_wake_source_classification_never_leaks_device_names(text: str, expected: WakeSource) -> None:
    assert classify_wake_source(text) is expected
    assert expected.value in {s.value for s in WakeSource}


RESUME_XML = """<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'>
<System><Provider Name='Microsoft-Windows-Power-Troubleshooter'/><EventID>1</EventID>
<TimeCreated SystemTime='2026-11-16T11:30:05.1234567Z'/></System>
<EventData>
<Data Name='SleepTime'>2026-11-16T10:00:00.000000000Z</Data>
<Data Name='WakeTime'>2026-11-16T11:30:00.500000000Z</Data>
<Data Name='WakeSourceText'>Wake Source: Device - Example Ethernet Adapter</Data>
</EventData></Event>"""


def test_resume_event_parsing_uses_wake_time_and_classifies_source() -> None:
    info = parse_resume_event_xml(RESUME_XML)
    assert info is not None
    assert info.at == datetime(2026, 11, 16, 11, 30, 0, 500000, tzinfo=UTC)
    assert info.source is WakeSource.NETWORK


def test_resume_event_falls_back_to_time_created() -> None:
    xml = RESUME_XML.replace("WakeTime", "Other").replace("Ethernet", "Unrecognised")
    info = parse_resume_event_xml(xml)
    assert info is not None and info.at.minute == 30 and info.at.second == 5
    assert info.source is WakeSource.UNKNOWN


def test_resume_event_garbage() -> None:
    assert parse_resume_event_xml("") is None
    assert parse_resume_event_xml("<Event><System/></Event>") is None


@pytest.mark.parametrize(
    ("mask", "expected"),
    [
        (0x0, ()),
        (0x1, ()),  # idle
        (0x2, ()),  # applications clocks setting
        (0x4, ("power",)),
        (0x20, ("thermal",)),
        (0x40 | 0x80, ("thermal", "power")),
        (0x8, ("other",)),
        (0x4 | 0x20 | 0x8, ("thermal", "power", "other")),
    ],
)
def test_throttle_classification(mask: int, expected: tuple[str, ...]) -> None:
    assert classify_throttle(mask) == expected


TYPEPERF = (
    '"(PDH-CSV 4.0)","\\\\HOST\\GPU Engine(pid_100_luid_0x0_0x1_phys_0_eng_0_engtype_3D)\\Utilization Percentage",'
    '"\\\\HOST\\GPU Engine(pid_100_luid_0x0_0x1_phys_0_eng_1_engtype_Copy)\\Utilization Percentage",'
    '"\\\\HOST\\GPU Engine(pid_200_luid_0x0_0x1_phys_0_eng_0_engtype_3D)\\Utilization Percentage"\r\n'
    '"11/16/2026 12:00:00.000","0.000000","0.000000","0.000000"\r\n'
    '"11/16/2026 12:00:01.000","12.500000","9.000000","3.000000"\r\n'
    "\r\nThe command completed successfully.\r\n"
)


def test_gpu_heavy_pids_sums_engines_per_pid_and_uses_last_sample() -> None:
    assert gpu_heavy_pids(TYPEPERF, 20.0) == {100: 21.5}
    assert gpu_heavy_pids(TYPEPERF, 1.0) == {100: 21.5, 200: 3.0}
    assert gpu_heavy_pids(TYPEPERF, 50.0) == {}


def test_gpu_heavy_pids_garbage() -> None:
    assert gpu_heavy_pids("", 1.0) == {}
    assert gpu_heavy_pids("Error: no valid counters.", 1.0) == {}
