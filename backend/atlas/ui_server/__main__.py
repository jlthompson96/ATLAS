"""atlas-ui command line::

    python -m atlas.ui_server serve --tls-cert CERT [--port 443] [--host IP ...] [--credentials-dir DIR]
    python -m atlas.ui_server pair --role kiosk|remote [--state-dir DIR]     # prints a one-time pairing code
    python -m atlas.ui_server devices [--state-dir DIR]                     # list paired devices
    python -m atlas.ui_server revoke DEVICE_ID [--state-dir DIR]
    python -m atlas.ui_server.pin                                           # hash a PIN for the stepup-pin-hash credential

``pair``, ``devices`` and ``revoke`` edit atlas-ui's state files directly (run them as ``atlas-ui`` or root); a
running server notices within seconds. They need no credentials: ``pair`` stores only a code hash and
``revoke`` flips a flag.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from collections.abc import Sequence
from pathlib import Path

from atlas.ui_server.app import build_server
from atlas.ui_server.config import ConfigError, credentials_dir, load_config
from atlas.ui_server.devices import PairingStore

DEFAULT_STATE = Path("/var/lib/atlas/atlas-ui")


def _state_dir(args: argparse.Namespace) -> Path:
    return Path(args.state_dir) if args.state_dir else DEFAULT_STATE


async def _serve(args: argparse.Namespace) -> int:
    try:
        cfg = load_config(
            credentials=credentials_dir(Path(args.credentials_dir) if args.credentials_dir else None),
            tls_cert=Path(args.tls_cert),
            port=args.port,
            state_dir=Path(args.state_dir) if args.state_dir else None,
            static_dir=Path(args.static_dir) if args.static_dir else None,
            core_socket=Path(args.core_socket) if args.core_socket else None,
            hosts=tuple(args.host or ["127.0.0.1"]),
            extra_origins=tuple(args.origin or []),
        )
    except ConfigError as exc:
        print(f"atlas-ui: {exc}", file=sys.stderr)
        return 2
    server = build_server(cfg)
    await server.start()
    logging.getLogger("atlas.ui").info("atlas-ui listening on %s:%d", ",".join(cfg.hosts), server.bound_port)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    await server.stop()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m atlas.ui_server", description="atlas-ui")
    sub = parser.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="run the server")
    serve.add_argument("--tls-cert", required=True)
    serve.add_argument("--port", type=int, default=443)
    serve.add_argument("--host", action="append", help="address to bind (repeatable; default 127.0.0.1)")
    serve.add_argument("--origin", action="append", help="extra allowed Origin, e.g. https://atlas.lan")
    serve.add_argument("--credentials-dir", default=None, help="default: $CREDENTIALS_DIRECTORY")
    serve.add_argument("--state-dir", default=None)
    serve.add_argument("--static-dir", default=None)
    serve.add_argument("--core-socket", default=None)

    pair = sub.add_parser("pair", help="print a one-time pairing code")
    pair.add_argument("--role", choices=["kiosk", "remote"], required=True)
    pair.add_argument("--state-dir", default=None)

    for name in ("devices", "revoke"):
        p = sub.add_parser(name)
        p.add_argument("--state-dir", default=None)
        if name == "revoke":
            p.add_argument("device_id")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.cmd == "serve":
        return asyncio.run(_serve(args))
    if args.cmd == "pair":
        print(PairingStore(_state_dir(args)).create(args.role))
        print("Valid for 10 minutes; single use.", file=sys.stderr)
        return 0
    # devices / revoke only need the registry's file; the key is not used for these operations.
    from atlas.ui_server.devices import DeviceRegistry

    registry = DeviceRegistry(_state_dir(args), os.urandom(32))
    if args.cmd == "devices":
        for device, revoked in registry.list():
            print(f"{device.device_id}  {device.role:6}  {'REVOKED' if revoked else 'active'}")
        return 0
    if registry.revoke(args.device_id):
        print(f"revoked {args.device_id}")
        return 0
    print("no such active device", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
