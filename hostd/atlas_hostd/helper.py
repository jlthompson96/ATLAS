"""``atlas-hostd-helper``: the per-user activity reporter (runs in the signed-in user's session).

Posts to ``/v1/helper/activity`` on the loopback listener every ``interval_s`` seconds and immediately when the
fullscreen flag changes or the user returns from idle. If hostd is not reachable it keeps trying quietly.
"""

from __future__ import annotations

import argparse
import http.client
import json
import logging
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from atlas_hostd.config import Config, ConfigError, load_config
from atlas_hostd.models import fmt_ts
from atlas_hostd.platform.base import HelperProbe

log = logging.getLogger("atlas_hostd.helper")

POST_TIMEOUT_S = 3.0
TICK_S = 1.0


def build_report(probe: HelperProbe, config: Config, now: datetime, *, heavy_count: int) -> dict[str, object]:
    return {
        "session_id": probe.session_id(),
        "input_idle_s": probe.input_idle_s(),
        "fullscreen_app": probe.fullscreen_app(),
        "gpu_heavy_process_count": heavy_count,
        "reported_at": fmt_ts(now),
    }


def post_report(port: int, report: dict[str, object]) -> bool:
    """POST to the loopback listener. Returns whether hostd answered 204."""
    body = json.dumps(report).encode()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=POST_TIMEOUT_S)
    try:
        conn.request("POST", "/v1/helper/activity", body, {"Content-Type": "application/json"})
        return conn.getresponse().status == 204
    except (OSError, http.client.HTTPException):
        return False
    finally:
        conn.close()


class HelperLoop:
    """The reporting policy, separated from sleeping and I/O so it can be tested with a fake probe and clock."""

    def __init__(self, probe: HelperProbe, config: Config, send: Callable[[dict[str, object]], bool]) -> None:
        self._probe = probe
        self._cfg = config
        self._send = send
        self._last_sent_at: float | None = None
        self._last_heavy_at: float | None = None
        self._heavy = 0
        self._prev_fullscreen: bool | None = None
        self._prev_idle: int | None = None

    def step(self, mono: float, wall: datetime) -> bool:
        """One tick. Returns whether a report was sent."""
        interval = self._cfg.helper.interval_s
        fullscreen = self._probe.fullscreen_app()
        idle = self._probe.input_idle_s()
        changed = (self._prev_fullscreen is not None and fullscreen != self._prev_fullscreen) or (
            self._prev_idle is not None and idle < self._prev_idle
        )
        self._prev_fullscreen, self._prev_idle = fullscreen, idle
        due = self._last_sent_at is None or mono - self._last_sent_at >= interval
        if not (due or changed):
            return False
        # The GPU probe is slow (a typeperf run), so it refreshes on the regular cadence only.
        if self._last_heavy_at is None or mono - self._last_heavy_at >= interval:
            self._heavy = self._probe.gpu_heavy_process_count(
                self._cfg.llm_server.all_process_names(), self._cfg.helper.gpu_heavy_pct
            )
            self._last_heavy_at = mono
        report = build_report(self._probe, self._cfg, wall, heavy_count=self._heavy)
        ok = self._send(report)
        self._last_sent_at = mono
        if not ok:
            log.warning("hostd did not accept the report")
        return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="atlas-hostd-helper", description="ATLAS per-user activity helper")
    parser.add_argument("config", type=Path, help="path to the TOML config file (same file hostd uses)")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"atlas-hostd-helper: {exc}", file=sys.stderr)
        return 2
    if sys.platform != "win32":
        print("atlas-hostd-helper: the real probe is Windows-only", file=sys.stderr)
        return 2
    from atlas_hostd.platform.windows import WindowsHelperProbe

    loop = HelperLoop(WindowsHelperProbe(), config, lambda r: post_report(config.helper.hostd_port, r))
    try:
        while True:
            loop.step(time.monotonic(), datetime.now(UTC))
            time.sleep(TICK_S)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
