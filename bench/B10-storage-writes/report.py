"""B10 report: turn sample.sh output into the bench/results/B10-<date>.md table.

    python3 report.py SAMPLES.log [--budget-mb 100]

Standard library only. MB here is 10^6 bytes; /proc/diskstats counts 512-byte sectors whatever the device's
sector size. These are host writes (what the kernel sent the card); the card's own write amplification is not
visible from the host.
"""

from __future__ import annotations

import argparse
import itertools
import sys
from dataclasses import dataclass, field
from pathlib import Path

SECTOR = 512
MB = 1_000_000
DAY = 86_400


@dataclass(frozen=True)
class Sample:
    epoch: int
    sectors_written: dict[str, int]
    writes_completed: dict[str, int]
    journal_kb: int | None


@dataclass(frozen=True)
class Run:
    header: dict[str, str]
    samples: list[Sample]
    ended: bool


@dataclass
class _Building:
    epoch: int
    sectors: dict[str, int] = field(default_factory=dict)
    writes: dict[str, int] = field(default_factory=dict)
    journal_kb: int | None = None


class SampleError(ValueError):
    pass


def parse(text: str) -> Run:
    header: dict[str, str] = {}
    samples: list[Sample] = []
    cur: _Building | None = None
    ended = False

    def close() -> None:
        if cur is not None:
            samples.append(Sample(cur.epoch, dict(cur.sectors), dict(cur.writes), cur.journal_kb))

    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        if line.startswith("# "):
            key, _, value = line[2:].partition(" ")
            if key == "end":
                ended = True
            header.setdefault(key, value)
            continue
        kind, *rest = line.split()
        if kind == "T":
            close()
            cur = _Building(int(rest[0]))
        elif kind == "D":
            if cur is None or len(rest) < 10:
                raise SampleError(f"line {n}: diskstats line outside a sample or too short")
            # major minor name reads rmerged rsectors rms writes wmerged wsectors ...
            cur.writes[rest[2]] = int(rest[7])
            cur.sectors[rest[2]] = int(rest[9])
        elif kind == "J":
            if cur is None:
                raise SampleError(f"line {n}: journal line outside a sample")
            cur.journal_kb = int(rest[0]) if rest else None
        else:
            raise SampleError(f"line {n}: unknown record {kind!r}")
    close()
    if len(samples) < 2:
        raise SampleError("need at least two samples")
    for a, b in itertools.pairwise(samples):
        if b.epoch <= a.epoch:
            raise SampleError("timestamps go backwards")
        for dev, sec in b.sectors_written.items():
            if sec < a.sectors_written.get(dev, 0):
                raise SampleError(f"{dev}: write counter went down (reboot?); start a new run")
    return Run(header, samples, ended)


def per_day(delta_bytes: int, seconds: int) -> float:
    return delta_bytes / MB * DAY / seconds


def report(run: Run, budget_mb: float) -> str:
    first, last = run.samples[0], run.samples[-1]
    seconds = last.epoch - first.epoch
    toks = run.header.get("interval", "").split()
    disk = toks[toks.index("disk") + 1] if "disk" in toks[:-1] else "mmcblk0"
    if disk not in last.sectors_written:
        raise SampleError(f"no samples for the whole disk {disk!r}")
    devices = sorted(last.sectors_written)

    lines = [
        "| Device | Written (MB) | Write requests | MB/day |",
        "|---|---:|---:|---:|",
    ]
    for dev in devices:
        written = (last.sectors_written[dev] - first.sectors_written.get(dev, 0)) * SECTOR
        reqs = last.writes_completed[dev] - first.writes_completed.get(dev, 0)
        lines.append(f"| `{dev}` | {written / MB:.1f} | {reqs} | {per_day(written, seconds):.1f} |")

    total = (last.sectors_written[disk] - first.sectors_written.get(disk, 0)) * SECTOR
    rate = per_day(total, seconds)

    worst_rate, worst_at = 0.0, first.epoch
    for a, b in itertools.pairwise(run.samples):
        r = per_day((b.sectors_written[disk] - a.sectors_written[disk]) * SECTOR, b.epoch - a.epoch)
        if r > worst_rate:
            worst_rate, worst_at = r, a.epoch

    hours = seconds / 3600
    verdict = "within" if rate <= budget_mb else "OVER"
    out = [
        f"Measured {hours:.1f} h over {len(run.samples) - 1} intervals"
        + ("" if run.ended else " (run did not reach its end marker)")
        + ".",
        "",
        *lines,
        "",
        f"**Whole card (`{disk}`): {rate:.1f} MB/day, {verdict} the {budget_mb:g} MB/day budget (PLAN 3.5).**",
        f"Busiest interval: {worst_rate:.1f} MB/day rate, starting at epoch {worst_at}.",
    ]
    if hours < 24:
        out.append(f"Shorter than 24 h ({hours:.1f} h): extrapolated, doesn't count for the exit criterion.")
    if first.journal_kb is not None and last.journal_kb is not None:
        grown = (last.journal_kb - first.journal_kb) * 1024
        out.append(
            f"Journal on disk: {first.journal_kb} KiB to {last.journal_kb} KiB ({per_day(grown, seconds):.2f} MB/day)."
        )
    out += ["", "Run header:", "", "```", *(f"{k} {v}" for k, v in run.header.items()), "```"]
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("samples", type=Path)
    ap.add_argument("--budget-mb", type=float, default=100.0)
    args = ap.parse_args(argv)
    try:
        run = parse(args.samples.read_text())
    except SampleError as e:
        print(f"B10: {e}", file=sys.stderr)
        return 1
    sys.stdout.write(report(run, args.budget_mb))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
