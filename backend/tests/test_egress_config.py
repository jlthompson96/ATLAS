"""Egress configuration and the address policy. The packaged allowlist must equal contracts/services.md."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest

from atlas.egress.addresses import is_public_address, normalize
from atlas.egress.config import (
    DEFAULT_CONFIG,
    ConfigError,
    is_valid_hostname,
    load_config,
    looks_like_ip_literal,
    parse_config,
)

SERVICES_MD = (Path(__file__).resolve().parents[2] / "contracts" / "services.md").read_text(encoding="utf-8")


def services_md_ports() -> dict[str, int]:
    section = SERVICES_MD.split("**CONNECT proxy ports**", 1)[1].split("Each service gets", 1)[0]
    return {user: int(port) for port, user in re.findall(r"^\| (\d+) \| `(atlas-[a-z]+)` \|", section, flags=re.M)}


def services_md_hosts() -> dict[str, set[str]]:
    section = SERVICES_MD.split("## 3. Internet egress allowlist", 1)[1].split("The OAuth consent flow", 1)[0]
    out: dict[str, set[str]] = {}
    for caller, mode, hosts in re.findall(
        r"^\| `(atlas-[a-z]+)` \| (CONNECT|research API) \| (.+?) \|", section, flags=re.M
    ):
        if mode == "CONNECT":
            out[caller] = set(re.findall(r"`([a-z0-9.-]+\.[a-z]+)`", hosts))
    return out


def test_allowlist_matches_services_md_exactly() -> None:
    config = load_config()
    assert {c: p.port for c, p in config.callers.items()} == services_md_ports()
    expected = services_md_hosts()
    assert set(config.callers) == set(expected)
    for caller, policy in config.callers.items():
        # atlas-search's engine hosts are chosen in Phase 5: services.md lists no literal hosts yet, so none are allowed.
        assert policy.hosts == frozenset(expected[caller]), caller
    assert config.callers["atlas-search"].hosts == frozenset()


def test_every_caller_is_an_audit_actor_and_a_pi_service() -> None:
    from atlas.audit.record import ACTORS

    assert set(load_config().callers) <= ACTORS


def test_proxy_listens_on_loopback_only() -> None:
    assert load_config().listen_host == "127.0.0.1"


def raw() -> dict[str, Any]:
    return json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(listen_host="0.0.0.0"),  # noqa: S104  (the test asserts it is refused)
        lambda d: d.update(version=2),
        lambda d: d.update(extra=1),
        lambda d: d["callers"]["atlas-google"].update(port=3132),  # duplicate port
        lambda d: d["callers"]["atlas-google"].update(port=80),
        lambda d: d["callers"]["atlas-google"]["hosts"].append("*.googleapis.com"),
        lambda d: d["callers"]["atlas-google"]["hosts"].append("10.0.0.1"),
        lambda d: d["callers"]["atlas-google"]["hosts"].append("Gmail.GoogleAPIs.com"),
        lambda d: d["callers"]["atlas-google"]["hosts"].append("example.com."),
        lambda d: d["callers"]["atlas-google"]["hosts"].append("gmail.googleapis.com"),  # duplicate
        lambda d: d["callers"].update({"google": {"port": 4000, "hosts": []}}),
        lambda d: d["callers"]["atlas-google"].update(extra=True),
        lambda d: d.update(callers={}),
    ],
)
def test_invalid_configs_fail_closed(mutate: Any) -> None:
    data = copy.deepcopy(raw())
    mutate(data)
    with pytest.raises(ConfigError):
        parse_config(data)


def test_unreadable_config_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(ConfigError):
        load_config(bad)


@pytest.mark.parametrize("name", ["gmail.googleapis.com", "a.b", "xn--bcher-kva.example", "a-b.example.org"])
def test_valid_hostnames(name: str) -> None:
    assert is_valid_hostname(name)


@pytest.mark.parametrize(
    "name",
    [
        "",
        "localhost",
        "-a.example",
        "a-.example",
        "a..b",
        "a.b.",
        "A.example",
        "*.example.com",
        "a_b.example",
        "x" * 64 + ".com",
        "1.2.3.4",
        "0x7f.1",
        "2130706433",
        "::1",
        "a b.example",
    ],
)
def test_invalid_hostnames(name: str) -> None:
    assert not is_valid_hostname(name)


@pytest.mark.parametrize(
    "name",
    ["127.0.0.1", "2130706433", "0x7f000001", "017700000001", "1.2.3", "::1", "[::1]", "::ffff:1.2.3.4", "example.0x1"],
)
def test_ip_literal_detection(name: str) -> None:
    assert looks_like_ip_literal(name)


@pytest.mark.parametrize("name", ["example.com", "1password.com", "a1.b2.example"])
def test_hostnames_are_not_ip_literals(name: str) -> None:
    assert not looks_like_ip_literal(name)


@pytest.mark.parametrize(
    "address",
    [
        "0.0.0.0",  # noqa: S104  (an address the policy must refuse, not a bind)
        "10.1.2.3", "100.64.0.1", "100.127.255.255", "127.0.0.1", "127.255.255.254", "169.254.169.254",
        "172.16.0.1", "172.31.255.255", "192.0.0.8", "192.0.2.1", "192.168.1.1", "198.18.0.1", "198.51.100.7",
        "203.0.113.9", "224.0.0.1", "239.255.255.250", "240.0.0.1", "255.255.255.255",
        "::", "::1", "fe80::1", "fe80::1%eth0", "fc00::1", "fd12:3456::1", "ff02::1", "2001:db8::1", "2002:7f00:1::1",
        "2001::1", "64:ff9b::7f00:1", "::ffff:127.0.0.1", "::ffff:10.0.0.1", "::ffff:169.254.169.254", "100::1", "fec0::1",
    ],
)  # fmt: skip
def test_forbidden_addresses(address: str) -> None:
    ip = normalize(address)
    assert ip is not None
    assert not is_public_address(ip)


@pytest.mark.parametrize(
    "address",
    [
        "8.8.8.8",
        "1.1.1.1",
        "93.184.216.34",
        "142.250.80.46",
        "2606:4700:4700::1111",
        "2a00:1450:4001::200e",
        "::ffff:8.8.8.8",
    ],
)
def test_public_addresses_are_allowed(address: str) -> None:
    ip = normalize(address)
    assert ip is not None
    assert is_public_address(ip)


@pytest.mark.parametrize("text", ["", "not-an-ip", "1.2.3", "999.1.1.1", "1.2.3.4.5"])
def test_unparseable_answers_are_not_addresses(text: str) -> None:
    assert normalize(text) is None
