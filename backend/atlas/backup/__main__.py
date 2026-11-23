"""``python -m atlas.backup run | status``.

run     one nightly backup (atlas-backup.service; needs CREDENTIALS_DIRECTORY and STATE_DIRECTORY from systemd)
status  exit 0 if the last run succeeded within --max-age-hours (default 36), else 1; prints one line
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from pathlib import Path

from atlas.backup.runner import Backup, Config, read_status, status_problem

DEFAULT_STATUS = Path("/var/lib/atlas/backup/status")
DRIVE_FILE = Path("/run/atlas-backup-mount/drive")  # written by atlas-backup-mount.service while the drive is mounted


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m atlas.backup", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    st = sub.add_parser("status")
    st.add_argument("--file", type=Path, default=DEFAULT_STATUS)
    st.add_argument("--max-age-hours", type=float, default=36.0)
    args = ap.parse_args(argv)

    if args.cmd == "status":
        try:
            status = read_status(args.file)
        except (OSError, ValueError, TypeError) as exc:
            print(f"no usable backup status at {args.file}: {type(exc).__name__}")
            return 1
        problem = status_problem(status, dt.datetime.now(dt.UTC), args.max_age_hours)
        print(problem or f"ok: last backup {status.time} drive={status.drive or '?'} snapshot={status.snapshot}")
        return 0 if problem is None else 1

    creds = os.environ.get("CREDENTIALS_DIRECTORY")
    state = os.environ.get("STATE_DIRECTORY")
    if not creds or not state:
        print(
            "run needs CREDENTIALS_DIRECTORY and STATE_DIRECTORY (start it through atlas-backup.service)",
            file=sys.stderr,
        )
        return 2
    cfg = Config(
        password_file=Path(creds) / "backup-repo-password",
        state_dir=Path(state),
        drive_file=DRIVE_FILE,
    )
    return 0 if Backup(cfg).run().ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
