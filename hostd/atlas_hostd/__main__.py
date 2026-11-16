"""``python -m atlas_hostd CONFIG.toml``: run the hostd listeners in the foreground.

Service hosting (Windows service wrapper vs Task Scheduler) is a Phase 1 decision; see hostd/README.md.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from atlas_hostd.config import ConfigError, load_config
from atlas_hostd.platform.base import Platform
from atlas_hostd.server import run_forever


def build_platform(*, fake: bool) -> Platform:
    if fake:
        from atlas_hostd.platform.fake import FakePlatform

        return FakePlatform()
    if sys.platform != "win32":
        raise SystemExit("the real platform layer is Windows-only; use --fake-platform for local development")
    from atlas_hostd.platform.windows import WindowsPlatform

    return WindowsPlatform()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="atlas-hostd", description="ATLAS LLM-host agent")
    parser.add_argument("config", type=Path, help="path to the TOML config file")
    parser.add_argument("--fake-platform", action="store_true", help="development only: simulated Windows layer")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"atlas-hostd: {exc}", file=sys.stderr)
        return 2
    platform = build_platform(fake=args.fake_platform)
    try:
        asyncio.run(run_forever(config, platform))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
