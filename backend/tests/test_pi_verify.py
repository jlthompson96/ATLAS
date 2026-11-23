"""deploy/provisioning/pi-verify.sh: read-only on the live Pi except its opt-in N4 restart, and it shows no secrets.

It runs as root on the Pi (and was run in the ARM64 VM); here the script text is pinned so it stays that way.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "provisioning" / "pi-verify.sh"
TEXT = SCRIPT.read_text()
SERVICE_BLOCK = TEXT.split('if [ "$SERVICE_CHECKS" = 1 ]; then', 1)[1].split("\nfi\n", 1)[0]
READ_ONLY_PART = TEXT.replace(SERVICE_BLOCK, "")
SHELL = "\n".join(
    line for line in READ_ONLY_PART.splitlines() if not line.lstrip().startswith(("#", "import", "q =", "s.", "print"))
)


# A command starts a line, or follows ; && || | ( `then`/`else`/`do`, or $( .
AT_COMMAND = r"(?:^|[;&|(]|\$\(|\bthen\b|\belse\b|\bdo\b|\bif\b|!)\s*"


def test_read_only_outside_the_service_checks() -> None:
    for verb in ("stop", "start", "restart", "enable", "disable", "reload", "mask", "kill", "reset-failed"):
        assert not re.search(rf"systemctl {verb}\b", SHELL), verb
    for cmd in (r"nft -f\b", r"nft (add|delete|flush|insert)\b", r"rm\b", r"mv\b", r"chattr\b", r"mkfs",
                r"umount\b", r"mount\b", r"install\b", r"write_file\b", r"tee\b", r"sed -i\b", r"chown\b",
                r"chmod\b", r"truncate\b", r"useradd\b", r"systemctl (?!show|is-)"):  # fmt: skip
        found = re.search(AT_COMMAND + cmd, SHELL, flags=re.M)
        assert not found, f"{cmd}: {found.group(0) if found else ''}"
    assert not re.search(
        r">\s*[\"/$]", SHELL.replace(">&2", "").replace("2>/dev/null", "").replace(">/dev/null", "")
    ), "no redirection into files"


def test_service_checks_only_restart_audit() -> None:
    calls = re.findall(r"systemctl (\w+) ([^\n;&|)]+)", SERVICE_BLOCK)
    assert [(verb, units.strip()) for verb, units in calls if verb not in ("show", "is-active")] == [
        ("restart", "atlas-audit.service")
    ]
    assert "--service-checks" in TEXT.split("set -euo pipefail", 1)[0]  # documented in the header


def test_no_secrets_are_read_out() -> None:
    assert ".cred" not in TEXT, "no credential file is named, let alone read"
    assert not re.search(r"\b(cat|head|tail|less|more|xxd|od|base64|strings|cp|scp)\b[^\n]*credential", TEXT)
    for leak in ("systemd-creds decrypt", "cat \"$DATA/credentials", "credentials/*", "wg show wg0 private-key",
                 "wg showconf", ".cred\"", "backup-repo-password"):  # fmt: skip
        assert leak not in TEXT, leak
    # the probes never print what came back
    assert "s.recv(512)\n" in TEXT and "print(s.recv" not in TEXT


def test_help_and_bad_arguments_without_root() -> None:
    ok = subprocess.run(["/bin/bash", str(SCRIPT), "--help"], capture_output=True, text=True, check=False)  # noqa: S603
    assert ok.returncode == 0 and "read-only" in ok.stdout
    bad = subprocess.run(["/bin/bash", str(SCRIPT), "--bogus"], capture_output=True, text=True, check=False)  # noqa: S603
    assert bad.returncode == 1 and "unknown argument" in bad.stderr


def test_ntp_probe_sends_a_real_request() -> None:
    assert 'if sys.argv[2] == "123":' in TEXT and "bytes([0x1B]) + bytes(47)" in TEXT
