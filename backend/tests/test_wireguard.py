"""WireGuard link, Pi side (PLAN 5.3, known issue D1): netdev generation, site parsing, key format, static files.

The behaviour under systemd-networkd, NetworkManager and the loaded ruleset was checked in the ARM64 VM (runbook
wireguard.md); these tests pin the files and helpers so that result keeps holding.
"""

from __future__ import annotations

import base64
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PROV = REPO / "deploy" / "provisioning"
WG = REPO / "deploy" / "wireguard"
SITE_EXAMPLE = REPO / "deploy" / "nftables" / "site.nft.example"
SERVICES_MD = (REPO / "contracts" / "services.md").read_text()
KEY = base64.b64encode(bytes(range(32))).decode()


def lib(snippet: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["/bin/bash", "-c", f'. "{PROV}/lib.sh"; {snippet}'],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": "/usr/bin:/bin", **env},
    )


def test_contract_values() -> None:
    assert "Pi `10.77.0.1`, desktop `10.77.0.2`" in SERVICES_MD and "UDP `51820`" in SERVICES_MD
    r = lib('printf "%s %s %s %s" "$WG_PI_IP" "$WG_DESKTOP_IP" "$WG_PORT" "$WG_CRED_NAME"')
    assert r.stdout == "10.77.0.1 10.77.0.2 51820 network.wireguard.private.wg0"


def test_site_define_reads_the_example() -> None:
    for name, want in (("DESKTOP_LAN_IP", "192.0.2.20"), ("WG_FWMARK", "0x5182"), ("DESKTOP_WG_IP", "10.77.0.2")):
        assert lib(f'site_define {name} "{SITE_EXAMPLE}"').stdout.strip() == want


@pytest.mark.parametrize(
    ("key", "ok"),
    [
        (KEY, True),
        (base64.b64encode(os.urandom(32)).decode(), True),
        (KEY[:-1], False),  # no padding
        (KEY.replace("=", "A"), False),
        (KEY[:-2] + "B=", False),  # impossible last character for 32 bytes
        ("", False),
        (KEY + "\nPrivateKey=x", False),
    ],
)
def test_wg_key_ok(key: str, ok: bool) -> None:
    assert (lib(f"wg_key_ok '{key}'").returncode == 0) is ok


def test_netdev_uses_the_networkd_key_and_a_credential() -> None:
    out = lib(f"wg_netdev_content 192.0.2.20 '{KEY}' 0x5182").stdout.splitlines()
    assert "[WireGuard]" in out and "[WireGuardPeer]" in out
    assert "FirewallMark=0x5182" in out  # known issue D1: networkd's key, matched by atlas.nft
    assert not any(line.startswith("FwMark") for line in out)
    assert "PrivateKey=@network.wireguard.private.wg0" in out  # a systemd-creds credential, never the key itself
    assert "ListenPort=51820" in out
    assert f"PublicKey={KEY}" in out and "AllowedIPs=10.77.0.2/32" in out and "Endpoint=192.0.2.20:51820" in out
    assert not any("PresharedKey" in line or "PersistentKeepalive" in line for line in out)


def test_fwmark_in_netdev_and_ruleset_come_from_one_define() -> None:
    nft = (REPO / "deploy" / "nftables" / "atlas.nft").read_text()
    assert "meta mark $WG_FWMARK ip daddr $DESKTOP_LAN_IP udp dport 51820 accept" in nft
    lib_text = (PROV / "lib.sh").read_text()
    assert 'mark="$(site_define WG_FWMARK "$site")"' in lib_text


def test_install_wireguard_waits_for_both_keys() -> None:
    r = lib(f'DRY_RUN=1 install_wireguard "{SITE_EXAMPLE}" "{WG}"', DRY_RUN="1")
    assert r.returncode == 0, r.stderr
    assert "wg0 not configured yet" in r.stderr
    assert "50-atlas-wg0.netdev" not in r.stderr and "systemctl" not in r.stderr


@pytest.mark.parametrize(
    ("define", "value", "msg"),
    [
        ("DESKTOP_LAN_IP", "desktop.example.org", "DESKTOP_LAN_IP must be a plain IPv4"),
        ("WG_FWMARK", "21890", "WG_FWMARK must be hexadecimal"),
        ("DESKTOP_WG_IP", "10.77.0.9", "DESKTOP_WG_IP must be 10.77.0.2"),
    ],
)
def test_install_wireguard_refuses_bad_site_values(tmp_path: Path, define: str, value: str, msg: str) -> None:
    site = tmp_path / "site.nft"
    lines = [
        f"define {define} = {value}" if ln.startswith(f"define {define} ") else ln
        for ln in SITE_EXAMPLE.read_text().splitlines()
    ]
    site.write_text("\n".join(lines) + "\n")
    r = lib(f'install_wireguard "{site}" "{WG}"', DRY_RUN="1")
    assert r.returncode == 1 and msg in r.stderr


def test_network_file() -> None:
    text = (WG / "wg0.network").read_text()
    for line in ("Name=wg0", "Address=10.77.0.1/32", "Destination=10.77.0.2/32", "RequiredForOnline=no"):
        assert f"\n{line}\n" in text
    assert "Gateway=" not in text and "DNS=" not in text and "0.0.0.0/0" not in text


def test_networkmanager_leaves_wg0_alone() -> None:
    assert "[keyfile]\nunmanaged-devices=interface-name:wg0\n" in (WG / "nm-unmanaged-wg0.conf").read_text()


def test_desktop_tunnel_example() -> None:
    text = (WG / "desktop-atlas.conf.example").read_text()
    active = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    assert "Address = 10.77.0.2/32" in active and "AllowedIPs = 10.77.0.1/32" in active
    assert "ListenPort = 51820" in active
    assert not any(ln.startswith(("PrivateKey", "DNS", "PresharedKey")) for ln in active)
    assert "Endpoint = 192.0.2.48:51820" in active  # RFC 5737 placeholder


def test_gen_wg_key_never_shows_the_private_key() -> None:
    text = (PROV / "gen-wg-key.sh").read_text()
    assert 'wg genkey >"$tmp/key"' in text
    assert 'systemd-creds encrypt --name="$WG_CRED_NAME" "$tmp/key" "$dest"' in text
    assert 'rm -f "$tmp/key"' in text and "trap 'rm -rf \"$tmp\"' EXIT" in text
    for leak in ('cat "$tmp/key"', 'echo "$key', "wg show", "private-key"):
        assert leak not in text, leak
    assert "umask 077" in text


def test_provisioning_order_and_packages() -> None:
    script = (PROV / "provision-pi.sh").read_text()
    assert script.index("install_firewall /etc/atlas") < script.index(
        'install_wireguard "$site_file" "$deploy_dir/wireguard"'
    )
    base_os = (PROV / "base-os.sh").read_text()
    assert "wireguard-tools" in base_os.split("PACKAGES=", 1)[1].split("\n", 1)[0]
    lib_text = (PROV / "lib.sh").read_text()
    assert "run systemctl disable systemd-networkd-wait-online.service" in lib_text
