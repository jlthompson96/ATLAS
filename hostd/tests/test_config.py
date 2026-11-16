"""Config loading, the example config, and the tracker/lease/helper units."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from atlas_hostd.activity import MAX_SESSIONS, ActivityTracker
from atlas_hostd.config import Config, ConfigError, load_config, parse_config
from atlas_hostd.helper import HelperLoop
from atlas_hostd.leases import LeaseLimitError, LeaseTable
from atlas_hostd.models import HelperReport, LeaseReason, LlmServerKind
from atlas_hostd.platform.fake import EPOCH, FakeHelperProbe

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.toml"


def test_example_config_loads_and_matches_services_md() -> None:
    cfg = load_config(EXAMPLE)
    assert (cfg.listener.address, cfg.listener.port, cfg.listener.allowed_source) == ("10.77.0.2", 8770, "10.77.0.1")
    assert (cfg.helper_listener.address, cfg.helper_listener.port) == ("127.0.0.1", 8771)
    assert cfg.sleep.input_idle_s == 1200 and cfg.llm_server.port == 1234


def test_defaults_equal_empty_config() -> None:
    assert parse_config({}) == Config()


@pytest.mark.parametrize(
    "raw",
    [
        {"listener": {"address": "0.0.0.0"}},
        {"listener": {"address": "::"}},
        {"listener": {"address": "wg0"}},
        {"listener": {"address": ""}},
        {"listener": {"port": 70000}},
        {"listener": {"allowed_source": "any"}},
        {"listener": {"bogus": 1}},
        {"helper_listener": {"address": "0.0.0.0"}},
        {"sleep": {"input_idle_s": 5}},
        {"sleep": {"allow_sleep_if_user_woke": "yes"}},
        {"sleep": {"input_idle_s": True}},
        {"llm_server": {"processes": {"ollama": ["x.exe"]}}},
        {"llm_server": {"processes": {"lmstudio": []}}},
        {"nope": {}},
        {"max_leases": 17},
    ],
)
def test_unsafe_or_unknown_config_is_rejected(raw: dict[str, object]) -> None:
    with pytest.raises(ConfigError):
        parse_config(raw)


def test_process_names_are_lowercased_and_merged_with_defaults() -> None:
    cfg = parse_config({"llm_server": {"processes": {"llmster": ["LLMster-Custom.EXE"]}}})
    assert cfg.llm_server.processes[LlmServerKind.LLMSTER] == ("llmster-custom.exe",)
    assert cfg.llm_server.processes[LlmServerKind.LMSTUDIO] == ("lm studio.exe",)


def test_load_config_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path / "missing.toml")
    bad = tmp_path / "bad.toml"
    bad.write_text("not = [valid")
    with pytest.raises(ConfigError):
        load_config(bad)


# ---- LeaseTable ----------------------------------------------------------------------------------------------


def test_lease_table_renewal_replaces_ttl_and_expiry_is_exclusive() -> None:
    table = LeaseTable(2)
    table.put("a", LeaseReason.BACKUP, 30, now_mono=0, now_wall=EPOCH)
    table.put("a", LeaseReason.RESEARCH_RUN, 100, now_mono=10, now_wall=EPOCH + timedelta(seconds=10))
    assert table.active(109)[0].reason is LeaseReason.RESEARCH_RUN
    assert table.active(110) == []  # expires exactly at expires_mono


def test_lease_table_limit_counts_only_live_leases() -> None:
    table = LeaseTable(1)
    table.put("a", LeaseReason.BACKUP, 30, now_mono=0, now_wall=EPOCH)
    with pytest.raises(LeaseLimitError):
        table.put("b", LeaseReason.BACKUP, 30, now_mono=1, now_wall=EPOCH)
    table.put("b", LeaseReason.BACKUP, 30, now_mono=31, now_wall=EPOCH)  # "a" expired
    assert [lease.lease_id for lease in table.active(31)] == ["b"]


def test_lease_table_rejects_out_of_range_ttl() -> None:
    table = LeaseTable(1)
    for ttl in (29, 1801):
        with pytest.raises(ValueError, match="ttl_s"):
            table.put("a", LeaseReason.BACKUP, ttl, now_mono=0, now_wall=EPOCH)


# ---- ActivityTracker -----------------------------------------------------------------------------------------


def _report(session: int, idle: int = 100, fullscreen: bool = False, heavy: int = 0) -> HelperReport:
    return HelperReport(session, idle, fullscreen, heavy, EPOCH)


def test_tracker_aggregates_conservatively_and_ignores_reported_at() -> None:
    tracker = ActivityTracker()
    tracker.record(_report(1, idle=500, heavy=1), now_mono=0, now_wall=EPOCH)
    tracker.record(_report(2, idle=50, fullscreen=True, heavy=2), now_mono=5, now_wall=EPOCH + timedelta(seconds=5))
    snap = tracker.snapshot(10)
    assert snap.helper_connected and snap.input_idle_s == 50 + 5  # session 2 was reported 5 s ago
    assert snap.fullscreen_app and snap.gpu_heavy_process_count == 3
    assert snap.reported_at == EPOCH + timedelta(seconds=5)


def test_tracker_bounds_sessions_and_expires_stale() -> None:
    tracker = ActivityTracker()
    for session in range(MAX_SESSIONS + 5):
        tracker.record(_report(session), now_mono=float(session) / 10, now_wall=EPOCH)
    assert len(tracker._sessions) == MAX_SESSIONS
    assert not tracker.snapshot(1000).helper_connected


# ---- helper loop ---------------------------------------------------------------------------------------------


def test_helper_loop_sends_every_interval_and_on_change() -> None:
    probe = FakeHelperProbe(idle_s=600)
    sent: list[dict[str, object]] = []
    loop = HelperLoop(probe, Config(), lambda r: sent.append(r) is None)
    assert loop.step(0, EPOCH)  # first tick always reports
    assert not loop.step(1, EPOCH)
    probe.fullscreen = True
    assert loop.step(2, EPOCH)  # fullscreen flipped
    probe.idle_s = 1
    assert loop.step(3, EPOCH)  # user came back
    probe.idle_s = 2
    assert not loop.step(4, EPOCH)  # idle growing normally
    assert loop.step(18, EPOCH)  # 15 s cadence
    assert len(sent) == 4
    assert sent[0].keys() == {"session_id", "input_idle_s", "fullscreen_app", "gpu_heavy_process_count", "reported_at"}


def test_helper_loop_survives_hostd_being_down() -> None:
    loop = HelperLoop(FakeHelperProbe(), Config(), lambda r: False)
    assert loop.step(0, EPOCH)
    assert not loop.step(1, EPOCH)
    assert loop.step(16, EPOCH)
