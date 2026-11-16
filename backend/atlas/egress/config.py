"""Egress configuration: one loopback listener per caller, each with an exact-match hostname allowlist.

The packaged ``allowlist.json`` is checked against contracts/services.md by
``tests/test_egress_config.py`` (callers, ports and hosts must match exactly), so the two cannot drift.
Validation is strict and fail-closed: any malformed entry aborts startup rather than being skipped.
"""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

DEFAULT_CONFIG: Final = Path(__file__).with_name("allowlist.json")
HTTPS_PORT: Final = 443

_LABEL: Final = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")


class ConfigError(Exception):
    """The egress configuration is invalid; the service must not start."""


def is_valid_hostname(name: str) -> bool:
    """Lower-case LDH hostname with at least two labels; no trailing dot, no wildcard, no IP literal."""
    if not name or len(name) > 253 or name != name.lower():
        return False
    labels = name.split(".")
    if len(labels) < 2 or not all(_LABEL.fullmatch(label) for label in labels):
        return False
    return not looks_like_ip_literal(name)


def looks_like_ip_literal(name: str) -> bool:
    """True for anything an IP stack or URL parser could read as an address (dotted, decimal, hex, IPv6)."""
    if ":" in name or name.startswith("["):
        return True
    try:
        ipaddress.ip_address(name)
    except ValueError:
        pass
    else:
        return True
    last = name.rsplit(".", 1)[-1]
    return last.isdigit() or last.startswith("0x")  # WHATWG: a numeric last label makes the host an IPv4 address


@dataclass(frozen=True)
class CallerPolicy:
    caller: str
    port: int
    hosts: frozenset[str]


@dataclass(frozen=True)
class EgressConfig:
    listen_host: str
    callers: dict[str, CallerPolicy]

    def by_port(self) -> dict[int, CallerPolicy]:
        return {policy.port: policy for policy in self.callers.values()}


def parse_config(data: Any) -> EgressConfig:
    if not isinstance(data, dict) or set(data) != {"version", "listen_host", "callers"} or data["version"] != 1:
        raise ConfigError("unexpected top-level structure or version")
    if data["listen_host"] != "127.0.0.1":
        raise ConfigError("the proxy may listen on 127.0.0.1 only")
    raw_callers = data["callers"]
    if not isinstance(raw_callers, dict) or not raw_callers:
        raise ConfigError("callers must be a non-empty object")
    callers: dict[str, CallerPolicy] = {}
    ports: set[int] = set()
    for name, entry in raw_callers.items():
        if not re.fullmatch(r"atlas-[a-z]+", name) or not isinstance(entry, dict) or set(entry) != {"port", "hosts"}:
            raise ConfigError(f"bad caller entry {name!r}")
        port, hosts = entry["port"], entry["hosts"]
        if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535 or port in ports:
            raise ConfigError(f"{name}: bad or duplicate port")
        if not isinstance(hosts, list) or len(set(hosts)) != len(hosts):
            raise ConfigError(f"{name}: hosts must be a list without duplicates")
        for host in hosts:
            if not isinstance(host, str) or not is_valid_hostname(host):
                raise ConfigError(f"{name}: invalid allowlist hostname {host!r}")
        ports.add(port)
        callers[name] = CallerPolicy(name, port, frozenset(hosts))
    return EgressConfig(listen_host=data["listen_host"], callers=callers)


def load_config(path: Path = DEFAULT_CONFIG) -> EgressConfig:
    try:
        return parse_config(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:  # ConfigError is neither, so it propagates unchanged
        raise ConfigError(f"cannot read {path.name}: {exc}") from exc
