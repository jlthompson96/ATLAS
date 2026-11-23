"""atlas-backup runner (ADR 0002): restic call sequence, retention, failure handling, status file, no leaks."""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from atlas.backup.__main__ import main
from atlas.backup.runner import (
    MONTHLY_READ,
    RETENTION,
    Backup,
    Config,
    Result,
    Status,
    backup_paths,
    parse_backup_json,
    read_status,
    status_problem,
)

MOCK_PASSWORD = "atlas-mock"  # noqa: S105  the repo-wide mock passphrase (CLAUDE.md)
PRIVATE_NAME = "private-name-of-a-memory-file.db"


class FakeRestic:
    def __init__(self, results: Mapping[str, Result] | None = None) -> None:
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self.results = dict(results or {})

    def __call__(self, argv: Sequence[str], env: Mapping[str, str]) -> Result:
        self.calls.append((list(argv), dict(env)))
        verb = argv[2]
        if verb in self.results:
            return self.results[verb]
        if verb == "backup":
            return Result(0, _summary(), "")
        return Result(0, "", "")

    def verbs(self) -> list[str]:
        return [argv[2] for argv, _ in self.calls]

    def argv(self, verb: str) -> list[str]:
        return next(a for a, _ in self.calls if a[2] == verb)


def _summary(**over: object) -> str:
    msg = {"message_type": "summary", "snapshot_id": "abc123", "files_new": 3, "files_changed": 1, "data_added": 4096}
    msg.update(over)
    return json.dumps({"message_type": "status", "percent_done": 1}) + "\n" + json.dumps(msg) + "\n"


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    etc = tmp_path / "etc-atlas"
    etc.mkdir()
    (etc / "services.conf").write_text("# comment\natlas-audit yes\natlas-egress yes\natlas-core no\natlas-backup no\n")
    root = tmp_path / "var-lib-atlas"
    for d in ("credentials", "audit", "egress", "backup", "releases", "logs", "incoming"):
        (root / d).mkdir(parents=True)
    host_key = tmp_path / "credential.secret"
    host_key.write_bytes(b"\0" * 32)
    credstore = tmp_path / "credstore.encrypted"
    credstore.mkdir()
    creds = tmp_path / "creds"
    creds.mkdir()
    (creds / "backup-repo-password").write_text(MOCK_PASSWORD)
    mount = tmp_path / "mnt"
    (mount / "restic").mkdir(parents=True)
    (mount / "restic" / "config").write_text("x")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "drive").write_text("A\n")
    return Config(
        password_file=creds / "backup-repo-password",
        state_dir=root / "backup",
        mount=mount,
        etc_atlas=etc,
        state_root=root,
        host_key=host_key,
        credstore=credstore,
        drive_file=run_dir / "drive",
        today=dt.date(2026, 11, 24),
    )


def _run(cfg: Config, fake: FakeRestic, *, mounted: bool = True) -> tuple[Status, list[str]]:
    logs: list[str] = []
    status = Backup(cfg, fake, logs.append).run(is_mount=lambda _p: mounted)
    return status, logs


def test_happy_path_sequence_and_status(cfg: Config) -> None:
    fake = FakeRestic()
    status, logs = _run(cfg, fake)
    assert fake.verbs() == ["unlock", "backup", "forget", "check"]  # repo exists: no init
    assert status.ok and status.step == "done"
    assert (status.drive, status.snapshot, status.files_new, status.bytes_added) == ("A", "abc123", 3, 4096)
    assert read_status(cfg.state_dir / "status") == status
    assert logs[-1].startswith("atlas-backup: ok drive=A snapshot=abc123")


def test_every_call_uses_no_cache_and_the_password_file_only(cfg: Config) -> None:
    fake = FakeRestic()
    _run(cfg, fake)
    for argv, env in fake.calls:
        assert argv[:2] == ["/usr/bin/restic", "--no-cache"]
        assert MOCK_PASSWORD not in " ".join(argv)
        assert MOCK_PASSWORD not in " ".join(env.values())
        assert env["RESTIC_PASSWORD_FILE"] == str(cfg.password_file)
        assert env["RESTIC_REPOSITORY"] == str(cfg.mount / "restic")
        assert "RESTIC_PASSWORD" not in env and "HOME" not in env


def test_backup_paths_follow_adr_0002(cfg: Config) -> None:
    paths = backup_paths(cfg)
    root = cfg.state_root
    assert paths == [
        cfg.etc_atlas,
        root / "credentials",
        root / "audit",
        root / "egress",
        root / "backup",  # core has no directory yet, so it is skipped
        cfg.host_key,
        cfg.credstore,  # the WireGuard key credential
    ]
    for excluded in ("releases", "logs", "incoming", "current", "previous"):
        assert root / excluded not in paths
    fake = FakeRestic()
    _run(cfg, fake)
    assert fake.argv("backup")[3:] == ["--json", "--tag", "nightly", *map(str, paths)]


def test_retention_is_14_daily_4_weekly_with_prune(cfg: Config) -> None:
    fake = FakeRestic()
    _run(cfg, fake)
    assert RETENTION == ("--keep-daily", "14", "--keep-weekly", "4")
    assert fake.argv("forget")[3:] == [*RETENTION, "--prune"]


def test_monthly_read_check_only_on_the_first(cfg: Config) -> None:
    fake = FakeRestic()
    _run(cfg, fake)
    assert fake.argv("check")[3:] == []
    first = Config(**{**cfg.__dict__, "today": dt.date(2026, 12, 1)})
    fake = FakeRestic()
    status, _ = _run(first, fake)
    assert fake.argv("check")[3:] == [MONTHLY_READ] == ["--read-data-subset=1/4"]
    assert status.monthly_read_check


def test_new_drive_gets_initialised(cfg: Config) -> None:
    (cfg.mount / "restic" / "config").unlink()
    fake = FakeRestic()
    status, logs = _run(cfg, fake)
    assert fake.verbs() == ["init", "unlock", "backup", "forget", "check"]
    assert status.ok
    assert any("initialised a new repository on drive A" in line for line in logs)


def test_not_mounted_fails_before_restic_runs(cfg: Config) -> None:
    fake = FakeRestic()
    status, logs = _run(cfg, fake, mounted=False)
    assert fake.calls == []
    assert not status.ok and status.step == "mount"
    assert read_status(cfg.state_dir / "status").step == "mount"
    assert "FAILED at mount" in logs[-1]


@pytest.mark.parametrize("content", [None, ""])
def test_missing_or_empty_password_fails(cfg: Config, content: str | None) -> None:
    if content is None:
        cfg.password_file.unlink()
    else:
        cfg.password_file.write_text(content)
    fake = FakeRestic()
    status, _ = _run(cfg, fake)
    assert fake.calls == [] and status.step == "password"


def test_missing_required_directory_fails(cfg: Config) -> None:
    (cfg.state_root / "credentials").rmdir()
    status, _ = _run(cfg, FakeRestic())
    assert not status.ok and status.step == "paths"


def test_host_key_and_credstore_are_optional(cfg: Config) -> None:
    cfg.host_key.unlink()
    cfg.credstore.rmdir()
    paths = backup_paths(cfg)
    assert cfg.host_key not in paths and cfg.credstore not in paths


def test_incomplete_snapshot_fails_and_logs_no_paths(cfg: Config) -> None:
    err = json.dumps({"message_type": "error", "error": {"message": "permission denied"}, "during": "archival",
                      "item": f"/var/lib/atlas/memory/{PRIVATE_NAME}"})  # fmt: skip
    fake = FakeRestic({"backup": Result(3, err + "\n" + _summary(), f"error: open {PRIVATE_NAME}: denied\n")})
    status, logs = _run(cfg, fake)
    assert not status.ok and status.step == "backup"
    assert status.source_errors == 1 and status.snapshot == "abc123"
    assert "forget" not in fake.verbs()  # an incomplete snapshot doesn't prune good ones
    assert PRIVATE_NAME not in "\n".join(logs)
    assert PRIVATE_NAME not in (cfg.state_dir / "status").read_text()


def test_error_messages_alone_also_fail(cfg: Config) -> None:
    err = json.dumps({"message_type": "error", "error": {"message": "x"}, "item": "/x"})
    status, _ = _run(cfg, FakeRestic({"backup": Result(0, err + "\n" + _summary(), "")}))
    assert not status.ok and status.source_errors == 1


@pytest.mark.parametrize("verb", ["unlock", "backup", "forget", "check"])
def test_any_restic_failure_stops_the_run(cfg: Config, verb: str) -> None:
    fake = FakeRestic({verb: Result(1, "", "Fatal: something broke\n")})
    status, logs = _run(cfg, fake)
    assert not status.ok and status.step == verb
    assert fake.verbs()[-1] == verb
    assert "restic exit 1: Fatal: something broke" in logs[-1]


def test_wrong_password_exit_code(cfg: Config) -> None:
    status, logs = _run(cfg, FakeRestic({"unlock": Result(12, "", "Fatal: wrong password or no key found\n")}))
    assert status.step == "unlock" and "restic exit 12" in logs[-1]


def test_parse_backup_json_tolerates_noise() -> None:
    summary, errors = parse_backup_json("not json\n{broken\n[1]\n" + _summary(snapshot_id="zz"))
    assert summary["snapshot_id"] == "zz" and errors == 0


def test_status_problem_freshness_and_failure() -> None:
    now = dt.datetime(2026, 11, 24, 12, 0, tzinfo=dt.UTC)
    ok = Status(time="2026-11-24T05:30:00Z", ok=True, step="done")
    assert status_problem(ok, now, 36) is None
    assert "more than 36 h" in (status_problem(ok, now + dt.timedelta(days=2), 36) or "")
    bad = Status(time="2026-11-24T05:30:00Z", ok=False, step="mount")
    assert "failed at step mount" in (status_problem(bad, now, 36) or "")


def test_status_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    f = tmp_path / "status"
    assert main(["status", "--file", str(f)]) == 1
    assert "no usable backup status" in capsys.readouterr().out
    fresh = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    f.write_text(json.dumps({"time": fresh, "ok": True, "step": "done", "drive": "B", "snapshot": "s1"}))
    assert main(["status", "--file", str(f)]) == 0
    assert "ok: last backup" in capsys.readouterr().out
    f.write_text("[]")
    assert main(["status", "--file", str(f)]) == 1


def test_run_cli_refuses_outside_systemd(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    monkeypatch.delenv("STATE_DIRECTORY", raising=False)
    assert main(["run"]) == 2
