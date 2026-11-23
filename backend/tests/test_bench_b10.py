"""B10 report parser (bench/B10-storage-writes/report.py): arithmetic, budget verdict, broken runs."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

BENCH = Path(__file__).resolve().parents[2] / "bench" / "B10-storage-writes"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("b10_report", BENCH / "report.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["b10_report"] = mod  # dataclasses look the module up by name
    spec.loader.exec_module(mod)
    return mod


b10 = _load()


def _d(name: str, writes: int, sectors: int) -> str:
    return f"D  179 0 {name} 10 0 100 5 {writes} 0 {sectors} 7 0 1 1 0 0 0 0 0 0"


def _log(points: list[tuple[int, int, int]], *, end: bool = True) -> str:
    """points: (epoch, sectors written on the disk, journal KiB); p3 gets every write."""
    out = ["# B10 storage writes", "# start 2026-11-23T15:00:00Z", "# interval 600 hours 24 disk mmcblk0"]
    for i, (t, sec, j) in enumerate(points):
        out += [f"T {t} {100 + i}.0", _d("mmcblk0", i * 3, sec), _d("mmcblk0p3", i * 3, sec), f"J {j}"]
    if end:
        out.append("# end 2026-11-24T15:00:00Z")
    return "\n".join(out) + "\n"


def test_24h_rate_and_partition_rows() -> None:
    # 50 MB over exactly 24 h = 50 MB/day
    sectors = 50_000_000 // 512
    run = b10.parse(_log([(0, 1000, 100), (43_200, 1000 + sectors // 2, 200), (86_400, 1000 + sectors, 300)]))
    text = b10.report(run, 100.0)
    assert "Measured 24.0 h over 2 intervals." in text
    assert "**Whole card (`mmcblk0`): 50.0 MB/day, within the 100 MB/day budget" in text
    assert "| `mmcblk0p3` | 50.0 | 6 | 50.0 |" in text
    assert "Shorter than 24 h" not in text
    assert "100 KiB to 300 KiB" in text


def test_over_budget_and_busiest_interval() -> None:
    quiet, burst = 1000, 1000 + 200_000_000 // 512  # 200 MB in the second 12 h
    run = b10.parse(_log([(0, quiet, 0), (43_200, quiet, 0), (86_400, burst, 0)]))
    text = b10.report(run, 100.0)
    assert "OVER the 100 MB/day budget" in text
    assert "Busiest interval: 400.0 MB/day rate, starting at epoch 43200." in text


def test_short_or_unfinished_run_is_flagged() -> None:
    text = b10.report(b10.parse(_log([(0, 0, 0), (3_600, 1000, 0)], end=False)), 100.0)
    assert "did not reach its end marker" in text
    assert "Shorter than 24 h (1.0 h)" in text


@pytest.mark.parametrize(
    ("log", "msg"),
    [
        (_log([(0, 5000, 0), (600, 4000, 0)]), "went down"),
        (_log([(600, 0, 0), (600, 1, 0)]), "backwards"),
        (_log([(0, 0, 0)]), "at least two"),
        ("T 1 1\nX what\n", "unknown record"),
        ("D 179 0 mmcblk0 1 2 3 4 5 6 7 8 9 10\n", "outside a sample"),
    ],
)
def test_broken_runs_are_rejected(log: str, msg: str) -> None:
    with pytest.raises(b10.SampleError, match=msg):
        b10.parse(log)


def test_cli_exit_codes(tmp_path: Path) -> None:
    good = tmp_path / "good.log"
    good.write_text(_log([(0, 0, 0), (86_400, 2048, 0)]))
    bad = tmp_path / "bad.log"
    bad.write_text("garbage\n")
    assert b10.main([str(good)]) == 0
    assert b10.main([str(bad)]) == 1


def test_sampler_rejects_bad_arguments() -> None:
    sh = shutil.which("sh")
    assert sh is not None
    for args in (["--interval", "x"], ["--hours", "0"], ["--bogus"]):
        r = subprocess.run([sh, str(BENCH / "sample.sh"), *args], capture_output=True, text=True, check=False)  # noqa: S603
        assert r.returncode == 2, (args, r.stderr)
