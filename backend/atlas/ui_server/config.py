"""atlas-ui configuration. Secrets come from systemd credentials (contracts/services.md section 4):
``tls-key``, ``device-token-key`` and ``stepup-pin-hash`` are files in ``$CREDENTIALS_DIRECTORY``."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from atlas.ui_server.security import default_origins

TLS_KEY_CRED = "tls-key"
DEVICE_KEY_CRED = "device-token-key"
PIN_HASH_CRED = "stepup-pin-hash"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class UiConfig:
    port: int
    tls_cert: Path
    tls_key: Path
    device_key: bytes = field(repr=False)
    pin_hash: str = field(repr=False)
    state_dir: Path = Path("/var/lib/atlas/atlas-ui")
    static_dir: Path = Path("/var/lib/atlas/current/hud")
    core_socket: Path = Path("/run/atlas/core.sock")
    #: Addresses to bind: the Pi's LAN IP and loopback (services.md section 2). Never 0.0.0.0 by default.
    hosts: tuple[str, ...] = ("127.0.0.1",)
    #: Extra Origins (``https://atlas.lan``) besides the loopback names.
    extra_origins: tuple[str, ...] = ()
    max_connections: int = 16

    @property
    def allowed_origins(self) -> frozenset[str]:
        return default_origins(self.port, self.extra_origins)


def read_credential(directory: Path, name: str) -> bytes:
    path = directory / name
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"credential {name!r} not readable in {directory}: {exc.strerror}") from None
    if not data.strip():
        raise ConfigError(f"credential {name!r} is empty")
    return data


def credentials_dir(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    env = os.environ.get("CREDENTIALS_DIRECTORY")
    if not env:
        raise ConfigError("no credentials directory: pass --credentials-dir or run under systemd (LoadCredential=)")
    return Path(env)


def load_config(
    *,
    credentials: Path,
    tls_cert: Path,
    port: int = 443,
    state_dir: Path | None = None,
    static_dir: Path | None = None,
    core_socket: Path | None = None,
    hosts: tuple[str, ...] = ("127.0.0.1",),
    extra_origins: tuple[str, ...] = (),
) -> UiConfig:
    defaults = UiConfig(port, tls_cert, credentials / TLS_KEY_CRED, b"", "")
    return UiConfig(
        port=port,
        tls_cert=tls_cert,
        tls_key=credentials / TLS_KEY_CRED,
        device_key=read_credential(credentials, DEVICE_KEY_CRED),
        pin_hash=read_credential(credentials, PIN_HASH_CRED).decode().strip(),
        state_dir=state_dir or defaults.state_dir,
        static_dir=static_dir or defaults.static_dir,
        core_socket=core_socket or defaults.core_socket,
        hosts=hosts,
        extra_origins=extra_origins,
    )
