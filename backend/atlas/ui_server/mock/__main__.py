"""python -m atlas.ui_server.mock [--scenario NAME|all|interactive] [--validate] ..."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys

from atlas.ui_server.mock import scenario as sc
from atlas.ui_server.mock.server import ALL, INTERACTIVE, Options, serve_forever
from atlas.ui_server.mock.session import FakeClock, Session
from atlas.ui_server.protocol import ContractViolation

VALIDATION_T0 = 1_763_279_000_000


async def validate_scenario(name: str, device_role: str = "remote") -> tuple[int, list[str]]:
    """Run a scenario instantly against the contract. Returns (frames, warnings); raises on violation."""
    frames: list[str] = []

    async def collect(frame: str) -> None:
        frames.append(frame)

    session = Session(collect, device_role=device_role, clock=FakeClock(VALIDATION_T0))
    scenario = sc.load(name)
    session.apply_setup(scenario)
    await session.snapshot()
    await session.play(scenario)
    return len(frames), session.checker.warnings


async def validate(names: list[str]) -> int:
    failed = 0
    for name in names:
        try:
            count, warnings = await validate_scenario(name)
        except (ContractViolation, ValueError, KeyError) as e:
            failed += 1
            print(f"FAIL  {name}: {e}")
            continue
        print(f"ok    {name}: {count} frames" + (f", {len(warnings)} warnings" if warnings else ""))
        for w in warnings:
            print(f"      warning: {w}")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m atlas.ui_server.mock", description=__doc__)
    ap.add_argument("--scenario", default=INTERACTIVE, help=f"scenario name, '{ALL}', or '{INTERACTIVE}' (default)")
    ap.add_argument("--loop", action="store_true", help="repeat the scenario(s) forever")
    ap.add_argument(
        "--speed", type=float, default=1.0, help="time multiplier, e.g. 4 to skip through the 60 s wake timeout"
    )
    ap.add_argument(
        "--device", choices=["remote", "kiosk"], default="remote", help="device role (kiosk can't approve or unlock)"
    )
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--list", action="store_true", help="list scenarios and exit")
    ap.add_argument("--validate", action="store_true", help="validate scenarios against events.v1 and exit")
    args = ap.parse_args(argv)

    if args.list:
        for name in sc.available():
            s = sc.load(name)
            print(f"{name:20} {s.duration_ms / 1000:6.1f} s  {s.description}")
        return 0
    if args.validate:
        names = sc.available() if args.scenario in (ALL, INTERACTIVE) else [args.scenario]
        return asyncio.run(validate([sc.BASELINE, *names]))
    if args.speed <= 0:
        ap.error("--speed must be positive")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("warning: the mock has no authentication; binding beyond loopback exposes it to the LAN", file=sys.stderr)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    opts = Options(args.host, args.port, args.scenario, args.loop, args.speed, args.device)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve_forever(opts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
