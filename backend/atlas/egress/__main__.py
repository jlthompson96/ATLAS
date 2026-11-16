"""``python -m atlas.egress``: run the CONNECT proxy (one loopback listener per caller)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from atlas.audit.client import DEFAULT_SOCKET, AuditClient
from atlas.egress.addresses import parse_networks
from atlas.egress.config import DEFAULT_CONFIG, ConfigError, load_config
from atlas.egress.proxy import EgressProxy


async def _run(config_path: Path, audit_socket: Path) -> int:
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        print(f"invalid egress configuration: {exc}", file=sys.stderr)
        return 2
    audit = AuditClient(audit_socket)
    try:
        lan_prefixes = parse_networks(os.environ.get("ATLAS_EGRESS_LAN_PREFIXES", ""))
    except ValueError as exc:
        print(f"invalid ATLAS_EGRESS_LAN_PREFIXES: {exc}", file=sys.stderr)
        return 2
    proxy = EgressProxy(config, audit, extra_forbidden=lan_prefixes)
    try:
        ports = await proxy.start()
    except OSError as exc:
        print(f"cannot bind the proxy listeners: {exc}", file=sys.stderr)
        return 2
    logging.getLogger("atlas.egress").info("listening on %s", {caller: ports[caller] for caller in sorted(ports)})
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    await proxy.close()
    await audit.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m atlas.egress", description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--audit-socket", type=Path, default=DEFAULT_SOCKET)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    return asyncio.run(_run(args.config, args.audit_socket))


if __name__ == "__main__":
    sys.exit(main())
