"""One backup run: unlock, init if new, backup, forget/prune, check, status file (ADR 0002).

Runs as ``atlas-backup`` (``CAP_DAC_READ_SEARCH`` only, no network). Root has already mounted one of the allowlisted
drives at ``/mnt/atlas-backup`` (``atlas-backup-mount.service``, which this unit requires). restic is called with the password in a file
(``RESTIC_PASSWORD_FILE`` from the systemd credential), never on the command line or in the environment.

What is logged: step names, exit codes, counts, sizes, durations and the snapshot ID. Never a backed-up path: restic's
per-file errors are counted, not printed (state paths can carry private names).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

MOUNT = Path("/mnt/atlas-backup")
RETENTION = ("--keep-daily", "14", "--keep-weekly", "4")  # ~5 weeks; bounds how long deleted data survives
MONTHLY_READ = "--read-data-subset=1/4"
# restic exit codes: 0 ok, 1 fatal, 3 snapshot written but some source files unreadable (incomplete), 10 no repo,
# 11 lock failed, 12 wrong password.
INCOMPLETE = 3

Runner = Callable[[Sequence[str], Mapping[str, str]], "Result"]


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class Config:
    password_file: Path
    state_dir: Path
    mount: Path = MOUNT
    etc_atlas: Path = Path("/etc/atlas")  # config, site firewall file, backup.conf, services.conf
    state_root: Path = Path("/var/lib/atlas")  # credentials/ and each service's state directory
    host_key: Path = Path("/var/lib/systemd/credential.secret")  # owner's decision 2026-11-23: back it up
    drive_file: Path | None = None  # written by atlas-backup-mount: the drive's name from backup.conf (A or B)
    restic: str = "/usr/bin/restic"
    today: dt.date = field(default_factory=lambda: dt.datetime.now(dt.UTC).date())

    @property
    def repo(self) -> Path:
        return self.mount / "restic"

    @property
    def services_conf(self) -> Path:
        return self.etc_atlas / "services.conf"


@dataclass(frozen=True)
class Status:
    time: str
    ok: bool
    step: str
    drive: str | None = None
    snapshot: str | None = None
    files_new: int | None = None
    files_changed: int | None = None
    bytes_added: int | None = None
    source_errors: int = 0
    duration_s: float = 0.0
    monthly_read_check: bool = False


class StepFailed(Exception):
    def __init__(self, step: str, detail: str) -> None:
        super().__init__(f"{step}: {detail}")
        self.step = step


def subprocess_runner(argv: Sequence[str], env: Mapping[str, str]) -> Result:
    p = subprocess.run(list(argv), env=dict(env), capture_output=True, text=True, check=False)  # noqa: S603
    return Result(p.returncode, p.stdout, p.stderr)


def service_state_dirs(services_conf: Path, root: Path) -> list[Path]:
    """/var/lib/atlas/<short> for every service in the root-owned services.conf whose directory exists."""
    out: list[Path] = []
    for line in services_conf.read_text(encoding="utf-8").splitlines():
        words = line.split()
        if not words or words[0].startswith("#"):
            continue
        name = words[0]
        if not (name.startswith("atlas-") and name[6:].isalpha() and name[6:].islower()):
            raise ValueError(f"bad service name in {services_conf}")
        d = root / name.removeprefix("atlas-")
        if d.is_dir():
            out.append(d)
    return out


def backup_paths(cfg: Config) -> list[Path]:
    """ADR 0002 section 4. Releases, current/previous, incoming and logs/ are left out (rebuilt from git, or diagnostic)."""
    fixed = [cfg.etc_atlas, cfg.state_root / "credentials"]
    missing = [p for p in fixed if not p.is_dir()]
    if missing:
        raise StepFailed(
            "paths", f"required director{'y' if len(missing) == 1 else 'ies'} missing: {', '.join(map(str, missing))}"
        )
    paths = [*fixed, *service_state_dirs(cfg.services_conf, cfg.state_root)]
    if cfg.host_key.is_file():
        paths.append(cfg.host_key)
    return paths


def _last_line(text: str, limit: int = 200) -> str:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    return lines[-1][:limit] if lines else ""


def parse_backup_json(stdout: str) -> tuple[dict[str, object], int]:
    """(summary message, number of per-file error messages) from ``restic backup --json``."""
    summary: dict[str, object] = {}
    errors = 0
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict):
            continue
        kind = msg.get("message_type")
        if kind == "summary":
            summary = msg
        elif kind == "error":
            errors += 1
    return summary, errors


def _int(v: object) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


class Backup:
    def __init__(self, cfg: Config, runner: Runner = subprocess_runner, log: Callable[[str], None] = print) -> None:
        self.cfg = cfg
        self.runner = runner
        self.log = log
        self.env = {
            "RESTIC_REPOSITORY": str(cfg.repo),
            "RESTIC_PASSWORD_FILE": str(cfg.password_file),
            "PATH": "/usr/bin:/bin",
            "TMPDIR": "/tmp",  # noqa: S108  the unit sets PrivateTmp=yes: a private tmpfs, never the card
        }

    def restic(self, step: str, *args: str, ok: tuple[int, ...] = (0,)) -> Result:
        r = self.runner([self.cfg.restic, "--no-cache", *args], self.env)
        if r.returncode not in ok:
            raise StepFailed(step, f"restic exit {r.returncode}: {_last_line(r.stderr)}")
        return r

    def run(self, is_mount: Callable[[Path], bool] = os.path.ismount) -> Status:
        start = time.monotonic()
        drive = None
        if self.cfg.drive_file is not None and self.cfg.drive_file.is_file():
            drive = self.cfg.drive_file.read_text(encoding="utf-8").strip()[:16] or None
        monthly = self.cfg.today.day == 1
        summary: dict[str, object] = {}
        errors = 0

        def status(ok: bool, step: str) -> Status:
            snap = summary.get("snapshot_id")
            return Status(
                time=_now(),
                ok=ok,
                step=step,
                drive=drive,
                snapshot=snap if isinstance(snap, str) else None,
                files_new=_int(summary.get("files_new")),
                files_changed=_int(summary.get("files_changed")),
                bytes_added=_int(summary.get("data_added")),
                source_errors=errors,
                duration_s=round(time.monotonic() - start, 1),
                monthly_read_check=monthly,
            )

        try:
            if not is_mount(self.cfg.mount):
                raise StepFailed("mount", f"{self.cfg.mount} is not a mount point (no backup drive mounted)")
            if not self.cfg.password_file.is_file() or self.cfg.password_file.stat().st_size == 0:
                raise StepFailed("password", "repository password credential missing or empty")
            paths = backup_paths(self.cfg)
            if not (self.cfg.repo / "config").exists():
                self.restic("init", "init")
                self.log(f"atlas-backup: initialised a new repository on drive {drive or '?'}")
            self.restic("unlock", "unlock")  # removes only stale locks (a power cut mid-run)
            r = self.restic("backup", "backup", "--json", "--tag", "nightly", *map(str, paths), ok=(0, INCOMPLETE))
            summary, errors = parse_backup_json(r.stdout)
            if r.returncode == INCOMPLETE or errors:
                raise StepFailed(
                    "backup", f"snapshot incomplete: {errors} file(s) could not be read (paths not logged)"
                )
            self.restic("forget", "forget", *RETENTION, "--prune")
            self.restic("check", "check", *([MONTHLY_READ] if monthly else []))
            result = status(True, "done")
            self.log(
                f"atlas-backup: ok drive={drive or '?'} snapshot={result.snapshot} new={result.files_new} "
                f"changed={result.files_changed} added={result.bytes_added}B duration={result.duration_s}s"
                + (" monthly-read-check" if monthly else "")
            )
        except StepFailed as exc:
            result = status(False, exc.step)
            self.log(f"atlas-backup: FAILED at {exc}")
        write_status(self.cfg.state_dir / "status", result)
        return result


def _now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_status(path: Path, status: Status) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(asdict(status), sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_status(path: Path) -> Status:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("status is not an object")
    return Status(**data)


def status_problem(status: Status, now: dt.datetime, max_age_hours: float) -> str | None:
    """None when the last run succeeded within max_age_hours; otherwise why not."""
    if not status.ok:
        return f"last run failed at step {status.step} ({status.time})"
    when = dt.datetime.strptime(status.time, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC)
    age = (now - when).total_seconds() / 3600
    if age > max_age_hours:
        return f"last success {age:.0f} h ago ({status.time}), more than {max_age_hours:g} h"
    return None
