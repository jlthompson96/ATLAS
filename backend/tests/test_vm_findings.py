"""Regression tests for the findings of the first ARM64 VM run (docs/known-issues.md V1, V4, V5).

Each failed before its fix. The shell parts run the real functions in bash with systemctl stubbed, as in
test_known_issues_deploy.py; the real commands were exercised in the VM (docs/runbooks/pi-verification.md).
"""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import pytest

from atlas.audit.log import AuditLog
from tests.test_audit_server import make_server, run, sock_path
from tests.test_known_issues_deploy import firewall, firewall_dirs
from tests.test_review_deploy import bash

__all__ = ["sock_path"]  # a pytest fixture

# --- V1: systemd-resolved (or a loopback-only resolv.conf) breaks every lookup under the firewall -------------------


def test_v1_firewall_is_refused_while_systemd_resolved_is_active(tmp_path: Path) -> None:
    src, etc = firewall_dirs(tmp_path)
    result, calls = firewall(tmp_path, src, etc, resolved_active=True)
    assert result.returncode != 0
    assert "systemd-resolved is active" in result.stderr and "resolv.conf" in result.stderr
    assert not [c for c in calls if c.startswith("nft") or "enable" in c or "reload" in c]
    assert not (etc / "atlas.nft").exists()


@pytest.mark.parametrize(
    "resolv_conf",
    [
        "nameserver 127.0.0.53\noptions edns0 trust-ad\n",  # resolved's stub, left behind after disabling it
        "nameserver ::1\n",
        "# no nameserver at all\n",
    ],
)
def test_v1_firewall_is_refused_with_a_loopback_or_missing_resolver(tmp_path: Path, resolv_conf: str) -> None:
    src, etc = firewall_dirs(tmp_path)
    result, _ = firewall(tmp_path, src, etc, resolv_conf=resolv_conf)
    assert result.returncode != 0
    assert "resolv.conf" in result.stderr and "LAN_DNS" in result.stderr


def test_v1_a_lan_resolver_passes_even_next_to_a_loopback_one(tmp_path: Path) -> None:
    src, etc = firewall_dirs(tmp_path)
    result, calls = firewall(tmp_path, src, etc, resolv_conf="nameserver 127.0.0.1\nnameserver 198.51.100.1\n")
    assert result.returncode == 0, result.stderr
    assert "systemctl enable --now nftables.service" in calls


# --- V5: a crash-looping unit must not pass the deploy health check --------------------------------------------------


def health(tmp_path: Path, states: str, restarts: str) -> tuple[int, str]:
    """Run atlas-release's health_check for one unit. ``states``: successive is-active answers (1 = active); the last
    repeats. ``restarts``: successive NRestarts values; the last repeats."""
    conf = tmp_path / "services.conf"
    conf.write_text("atlas-audit yes\n")
    result = bash(
        f"""
CONF="{conf}"; HEALTH_TIMEOUT=3; HEALTH_STABLE=0
states=({states}); restarts=({restarts}); si=0; echo 0 >"{tmp_path}/ri"
sleep() {{ :; }}
systemctl() {{
  local ri
  case "$1" in
    is-active) local s=${{states[$si]}}; [ $((si + 1)) -lt ${{#states[@]}} ] && si=$((si + 1)); [ "$s" = 1 ] ;;
    show)  # runs inside $(...): the position lives in a file, not a variable
      ri=$(cat "{tmp_path}/ri"); echo "${{restarts[$ri]}}"
      if [ $((ri + 1)) -lt ${{#restarts[@]}} ]; then echo $((ri + 1)) >"{tmp_path}/ri"; fi ;;
  esac
}}
health_check
"""
    )
    return result.returncode, result.stderr


def test_v5_a_unit_that_restarts_after_becoming_active_is_unhealthy(tmp_path: Path) -> None:
    code, err = health(tmp_path, "1", "0 1")  # active when first asked, but Restart= has fired by the second look
    assert code != 0 and "restarted" in err


def test_v5_a_unit_that_stops_after_becoming_active_is_unhealthy(tmp_path: Path) -> None:
    code, err = health(tmp_path, "1 0", "0")
    assert code != 0 and "stopped" in err


def test_v5_a_unit_that_stays_up_is_healthy(tmp_path: Path) -> None:
    code, err = health(tmp_path, "0 0 1", "2")  # slow to start is fine; a steady restart count is what matters
    assert code == 0, err


def test_v5_the_stability_window_is_set() -> None:
    text = (Path(__file__).resolve().parents[2] / "deploy" / "provisioning" / "atlas-release.sh").read_text()
    assert "HEALTH_STABLE=10" in text


# --- V4: the audit server must not unlink a socket that systemd owns -------------------------------------------------


def test_v4_closing_a_socket_activated_server_leaves_the_socket_path(make_log: object, sock_path: Path) -> None:
    async def scenario() -> None:
        log: AuditLog = make_log()  # type: ignore[operator]
        server, _ = make_server(log)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)  # what systemd's socket unit hands over
        listener.bind(str(sock_path))
        listener.listen()
        await server.listen_socket(listener)
        try:
            await asyncio.wait_for(server.close(), 5)
        finally:
            log.close()

    run(scenario())
    assert sock_path.exists()  # still systemd's; the old code unlinked it on close (when it was allowed to)
