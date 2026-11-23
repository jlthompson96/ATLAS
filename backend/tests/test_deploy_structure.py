"""Static checks of deploy/: systemd units, socket units, nftables rules and provisioning scripts against
contracts/services.md. These cannot prove the Linux behaviour (that needs the ARM64 Debian VM, see
docs/runbooks/pi-verification.md); they stop drift between the contract and the files that implement it."""

from __future__ import annotations

import ipaddress
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from atlas.audit.record import ACTORS
from atlas.egress.addresses import _FORBIDDEN_V4, _FORBIDDEN_V6, is_public_address
from tests.test_contracts import _services_md_pi_services

REPO = Path(__file__).resolve().parents[2]
DEPLOY = REPO / "deploy"
SYSTEMD = DEPLOY / "systemd"
SERVICES_MD = (REPO / "contracts" / "services.md").read_text(encoding="utf-8")

PI_SERVICES = sorted(_services_md_pi_services())
# Services that never write audit records: the kiosk (a browser), and the backup (no socket, no network; ADR 0002).
NON_AUDIT = {"atlas-kiosk", "atlas-backup"}
# Root helper units that belong to a service but are not service users (services.md section 1, atlas-backup row).
HELPER_UNITS = ["atlas-backup-mount"]


# --- parsing services.md -----------------------------------------------------------------------------


def section(start: str, end: str) -> str:
    return SERVICES_MD.split(start, 1)[1].split(end, 1)[0]


def md_proxy_ports() -> dict[str, int]:
    text = section("**CONNECT proxy ports**", "Each service gets")
    return {user: int(port) for port, user in re.findall(r"^\| (\d+) \| `(atlas-[a-z]+)` \|", text, flags=re.M)}


def md_credentials() -> dict[str, set[str]]:
    text = section("## 4. Secrets", "The WireGuard private key")
    out: dict[str, set[str]] = {}
    for services, creds in re.findall(r"^\| ((?:`atlas-[a-z]+`(?:, )?)+) \| (.+?) \|$", text, flags=re.M):
        names = set(re.findall(r"`(atlas-[a-z]+)`", services))
        if "none" in creds:
            found: set[str] = set()
        else:
            found = {c for c in re.findall(r"`([a-z0-9-]+)`", creds) if not c.startswith("atlas-")}
            found -= {"gmail.readonly", "calendar.readonly", "gmail.send"}  # scopes named in prose, not credentials
            found = {c for c in found if "-" in c}
        for name in names:
            out[name] = found
    return out


def md_sockets() -> dict[str, tuple[str, set[str] | None]]:
    """socket path -> (owner service, single-consumer set or None when several/any)."""
    text = section("## 2. Listeners and ports", "**CONNECT proxy ports**")
    out: dict[str, tuple[str, set[str] | None]] = {}
    for first, path, consumers in re.findall(
        r"^\| (.+?) \| `(/run/atlas/[a-z-]+\.sock)` \| (.+?) \|", text, flags=re.M
    ):
        owner = re.findall(r"`(atlas-[a-z]+)`", first)[0]
        names = set(re.findall(r"`(atlas-[a-z]+)`", consumers))
        out[path] = (owner, names if len(names) == 1 else None)
    return out


def md_baseline() -> dict[str, str]:
    block = section("## 5. Per-unit sandbox baseline", "`atlas-research` adds").split("```ini", 1)[1].split("```", 1)[0]
    out: dict[str, str] = {}
    for raw in block.splitlines():
        line = raw.split("#", 1)[0].strip()
        if "=" in line:
            key, value = line.split("=", 1)
            out[key] = value.strip()
    return out


# --- parsing units -----------------------------------------------------------------------------------


class Unit:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: list[tuple[str, str, str]] = []  # (section, key, value)
        self.comments: list[str] = []
        current = ""
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("#"):
                self.comments.append(line)
            elif line.startswith("["):
                current = line.strip("[]")
            else:
                key, _, value = line.partition("=")
                self.entries.append((current, key, value))

    def values(self, key: str, sect: str = "Service") -> list[str]:
        return [v for s, k, v in self.entries if s == sect and k == key]

    def one(self, key: str, sect: str = "Service") -> str | None:
        found = self.values(key, sect)
        assert len(found) <= 1, f"{self.path.name}: {key} repeated"
        return found[0] if found else None

    def words(self, key: str, sect: str = "Service") -> set[str]:
        return {w for v in self.values(key, sect) for w in v.split()}

    def relaxed(self, key: str) -> bool:
        return any(c.startswith(f"# RELAXED {key}") for c in self.comments)


def unit(name: str) -> Unit:
    return Unit(SYSTEMD / f"{name}.service")


# --- systemd services ----------------------------------------------------------------------------------


def test_one_unit_per_pi_service_and_no_strays() -> None:
    assert len(PI_SERVICES) == 15
    assert sorted(p.stem for p in SYSTEMD.glob("*.service")) == sorted(PI_SERVICES + HELPER_UNITS)


def test_services_conf_lists_exactly_the_pi_services() -> None:
    lines = [
        ln.split()
        for ln in (DEPLOY / "provisioning" / "services.conf").read_text().splitlines()
        if ln.strip() and not ln.startswith("#")
    ]
    assert sorted(name for name, _ in lines) == PI_SERVICES
    assert {name for name, enable in lines if enable == "yes"} == {"atlas-audit", "atlas-egress"}  # Phase 0-1 only


@pytest.mark.parametrize("name", PI_SERVICES)
def test_user_group_and_state_directory(name: str) -> None:
    u = unit(name)
    assert u.one("User") == name
    assert u.one("Group") == name
    assert u.one("DynamicUser") == "no"
    assert u.one("StateDirectory") == f"atlas/{name.removeprefix('atlas-')}"
    assert u.one("StateDirectoryMode") == "0700"
    assert (name in ACTORS) != (name in NON_AUDIT)


# Who may deviate from which baseline directive (services.md section 5 comments + the documented needs of each service).
ALLOWED_RELAXATIONS: dict[str, set[str]] = {
    "atlas-voice": {"PrivateDevices"},  # MemoryDenyWriteExecute stays yes until measured (TODO(VM) in the unit)
    "atlas-kiosk": {"PrivateDevices"},
}


def md_family_relaxations() -> dict[str, tuple[set[str], str]]:
    """service -> (address families it adds, IPAddressAllow description), from the services.md 1.1.0 table."""
    text = section("**Address-family relaxations**", "**Capabilities:**")
    out: dict[str, tuple[set[str], str]] = {}
    for names, adds, allow in re.findall(
        r"^\| ((?:`atlas-[a-z]+`(?:, )?)+) \| `([A-Z0-9_ ]+)` \| (.+?) \|$", text, flags=re.M
    ):
        for name in re.findall(r"`(atlas-[a-z]+)`", names):
            out[name] = (set(adds.split()), allow)
    return out


def md_capabilities() -> dict[str, str]:
    text = section("**Capabilities:**", "\n\n")
    return dict(re.findall(r"`(atlas-[a-z]+)` gets `(CAP_[A-Z_]+)`", text))


def md_socket_groups() -> dict[str, str]:
    text = section("| Socket | Group | Members |", "A group named")
    return {
        f"/run/atlas/{sock}": group
        for sock, group in re.findall(r"^\| `([a-z-]+\.sock)` \| `([a-z-]+)` \|", text, flags=re.M)
    }


def md_reserved_names() -> set[str]:
    text = section("Reserved names under `/var/lib/atlas/`", "- Each service's runtime")
    return set(re.findall(r"^  - `([a-z]+)`:", text, flags=re.M))


FAMILIES = md_family_relaxations()
INET_SERVICES = set(FAMILIES)


@pytest.mark.parametrize("name", PI_SERVICES)
def test_sandbox_baseline_present_and_every_relaxation_documented(name: str) -> None:
    u = unit(name)
    baseline = md_baseline()
    assert {"NoNewPrivileges", "ProtectSystem", "PrivateDevices", "SystemCallFilter", "IPAddressDeny"} <= set(baseline)
    for key, expected in baseline.items():
        if key in ("User", "StateDirectory"):
            continue  # per-service templates, checked above
        actual = u.one(key)
        if key == "RestrictAddressFamilies":
            continue  # checked below
        if key == "IPAddressDeny":
            assert actual == "any", "every unit denies all addresses first"
            continue
        if actual != expected:
            assert key in ALLOWED_RELAXATIONS.get(name, set()), f"{name} relaxes {key} without being allowed to"
            assert u.relaxed(key), f"{name}: {key} deviates from the baseline but is not marked '# RELAXED {key}'"
    for key in ALLOWED_RELAXATIONS.get(name, set()):
        if u.one(key) != baseline[key]:
            assert u.relaxed(key)


@pytest.mark.parametrize("name", PI_SERVICES)
def test_network_scope(name: str) -> None:
    u = unit(name)
    families = u.words("RestrictAddressFamilies")
    assert "AF_UNIX" in families
    inet = families & {"AF_INET", "AF_INET6"}
    assert bool(inet) == (name in INET_SERVICES), f"{name}: IP families {inet}"
    if name in FAMILIES:
        assert families == {"AF_UNIX"} | FAMILIES[name][0], f"{name} must have exactly the services.md families"
    if inet:
        assert u.relaxed("RestrictAddressFamilies")
    allow = u.words("IPAddressAllow")
    if name == "atlas-egress":
        assert allow == {"any"}
        assert u.relaxed("IPAddressAllow")
    elif name in ("atlas-research", "atlas-backup"):
        assert u.one("PrivateNetwork") == "yes"
        assert not allow and not inet
    elif inet:
        assert "any" not in allow and "localhost" in allow
        assert u.relaxed("IPAddressAllow")
    else:
        assert not allow
    if name in md_proxy_ports():
        assert allow == {"localhost"}, "proxy callers may only talk to loopback"
    if name == "atlas-core":
        assert allow == {"localhost", "10.77.0.2", "255.255.255.255"}  # the LAN broadcast comes from a drop-in


@pytest.mark.parametrize("name", PI_SERVICES)
def test_credentials_match_services_md_exactly(name: str) -> None:
    u = unit(name)
    declared = u.values("LoadCredentialEncrypted")
    names = {d.split(":", 1)[0] for d in declared}
    assert names == md_credentials()[name]
    assert len(names) == len(declared)
    for entry in declared:
        cred, path = entry.split(":", 1)
        assert path == f"/var/lib/atlas/credentials/{name}/{cred}.cred"
    assert not u.values("LoadCredential") and not u.values("SetCredential") and not u.values("EnvironmentFile")


def test_credentials_table_was_parsed() -> None:
    creds = md_credentials()
    assert creds["atlas-audit"] == {"audit-checkpoint-key", "audit-segment-master-key"}
    assert creds["atlas-executor"] == {"google-oauth-client", "google-send-refresh-token", "discord-poster-bot-token"}
    assert creds["atlas-egress"] == set() == creds["atlas-search"]
    assert set(creds) == set(PI_SERVICES)


@pytest.mark.parametrize("name", PI_SERVICES)
def test_https_proxy_matches_the_callers_port(name: str) -> None:
    envs = [e for e in unit(name).values("Environment") if e.startswith(("HTTPS_PROXY=", "https_proxy=", "ALL_PROXY="))]
    ports = md_proxy_ports()
    if name in ports:
        assert envs == [f"HTTPS_PROXY=http://127.0.0.1:{ports[name]}"]
    else:
        assert envs == []


def test_proxy_ports_cover_the_expected_callers() -> None:
    assert md_proxy_ports() == {
        "atlas-google": 3131,
        "atlas-discord": 3132,
        "atlas-executor": 3133,
        "atlas-panels": 3134,
        "atlas-market": 3135,
        "atlas-search": 3136,
    }


@pytest.mark.parametrize("name", PI_SERVICES)
def test_code_runs_through_the_current_symlink(name: str) -> None:
    exec_start = unit(name).one("ExecStart")
    assert exec_start is not None
    assert exec_start.startswith("/var/lib/atlas/current/")
    assert "/releases/" not in exec_start
    assert all("/releases/" not in e for e in unit(name).values("Environment"))


@pytest.mark.parametrize("name", PI_SERVICES)
def test_hardening_extras_and_no_secrets_in_units(name: str) -> None:
    u = unit(name)
    caps = md_capabilities()
    assert caps == {  # table was parsed
        "atlas-audit": "CAP_LINUX_IMMUTABLE",
        "atlas-ui": "CAP_NET_BIND_SERVICE",
        "atlas-backup": "CAP_DAC_READ_SEARCH",
    }
    assert u.one("CapabilityBoundingSet") == caps.get(name, "")
    assert u.one("AmbientCapabilities") == (caps.get(name) or None)
    assert u.one("UMask") == "0077"
    text = u.path.read_text(encoding="utf-8")
    assert not re.search(r"(?i)(token|secret|password)=\S{8,}", text)


def test_budgeted_services_set_memory_max() -> None:
    assert unit("atlas-voice").one("MemoryMax") == "1300M"  # 150 + 800 + 300 MB, PLAN 3.1
    assert unit("atlas-fallback").one("MemoryMax") == "1G"


def test_audit_unit_specifics() -> None:
    u = unit("atlas-audit")
    assert u.values("Requires", "Unit").count("atlas-audit.socket") == 1
    assert "ATLAS_AUDIT_REQUIRE_APPEND_ONLY=1" in u.values("Environment")
    assert u.one("ExecStart") == "/var/lib/atlas/current/venv/atlas-audit/bin/python -m atlas.audit serve"
    assert u.one("Restart") == "always"


def test_backup_unit_specifics() -> None:
    """ADR 0002: oneshot from a timer, root only to mount and unmount, writes only the drive and its state directory."""
    u = unit("atlas-backup")
    assert u.one("Type") == "oneshot"
    assert u.one("ExecStart") == "/var/lib/atlas/current/venv/atlas-backup/bin/python -m atlas.backup run"
    assert "atlas-backup-mount.service" in u.words("Requires", "Unit")
    assert "atlas-backup-mount.service" in u.words("After", "Unit")
    assert u.values("ReadWritePaths") == ["/mnt/atlas-backup"]
    assert u.one("Restart") is None
    assert not u.values("SupplementaryGroups")
    assert not u.values("WantedBy", "Install"), "started by atlas-backup.timer, not at boot"
    assert "atlas-audit.socket" not in u.words("Requires", "Unit")
    # A '+' or '!' line runs outside the sandbox: no service unit has one (root work goes in a helper unit).
    for name in PI_SERVICES:
        for key in ("ExecStart", "ExecStartPre", "ExecStartPost", "ExecCondition", "ExecStop", "ExecStopPost"):
            plus = [v for v in unit(name).values(key) if v.lstrip("-@:").startswith(("+", "!"))]
            assert not plus, f"{name} {key} runs privileged"


def test_backup_mount_helper_unit() -> None:
    """Root, host mount namespace (no namespacing options), mounted only while atlas-backup runs."""
    u = unit("atlas-backup-mount")
    assert u.one("User") is None, "runs as root: it mounts"
    assert (u.one("Type"), u.one("RemainAfterExit")) == ("oneshot", "yes")
    assert u.one("StopWhenUnneeded", "Unit") == "yes"
    assert u.one("ExecStart") == "/usr/local/sbin/atlas-backup-mount mount"
    assert u.one("ExecStop") == "/usr/local/sbin/atlas-backup-mount unmount"
    assert u.one("RuntimeDirectory") == "atlas-backup-mount"
    namespacing = {
        "ProtectSystem", "ProtectHome", "PrivateTmp", "PrivateDevices", "PrivateMounts", "ReadWritePaths",
        "ReadOnlyPaths", "InaccessiblePaths", "TemporaryFileSystem", "BindPaths", "BindReadOnlyPaths", "RootDirectory",
        "MountAPIVFS", "ProtectKernelTunables", "ProtectKernelModules", "ProtectControlGroups", "ProtectProc",
        "StateDirectory", "DynamicUser", "PrivateNetwork",  # PrivateNetwork: own mount namespace on systemd 257
    }  # fmt: skip
    present = {k for s, k, _ in u.entries if s == "Service"} & namespacing
    assert not present, f"would hide the mount in a private namespace: {present}"
    assert u.one("IPAddressDeny") == "any" and u.one("NoNewPrivileges") == "yes"
    assert not u.values("WantedBy", "Install") and not u.values("Install", "Install")


def test_backup_timer() -> None:
    timer = Unit(SYSTEMD / "atlas-backup.timer")
    assert timer.one("OnCalendar", "Timer") == "*-*-* 05:30:00"  # PLAN 2.7
    assert timer.one("Unit", "Timer") == "atlas-backup.service"
    assert timer.one("Persistent", "Timer") == "true"
    assert timer.one("WantedBy", "Install") == "timers.target"
    assert sorted(p.name for p in SYSTEMD.glob("*.timer")) == ["atlas-backup.timer"]


def test_provisioning_installs_backup_pieces_and_gates_the_timer() -> None:
    script = (DEPLOY / "provisioning" / "provision-pi.sh").read_text()
    assert '"$deploy_dir"/systemd/*.timer' in script
    assert 'install_file "$here/atlas-backup-mount.sh" /usr/local/sbin/atlas-backup-mount 755' in script
    assert "ensure_dir /mnt/atlas-backup 0755 root:root" in script
    gate = script.split("run systemctl enable --now atlas-backup.timer")[0].rsplit("\nif ", 1)[1]
    assert "$BACKUP_CONF" in gate and '-f "$BACKUP_CRED"' in gate
    assert 'write_file "$BACKUP_CONF"' in script.split('if [ -e "$BACKUP_CONF" ]; then')[1].split("fi\n")[0]
    base_os = (DEPLOY / "provisioning" / "base-os.sh").read_text()
    assert "restic" in base_os.split("PACKAGES=", 1)[1].split("\n", 1)[0]


def test_egress_unit_specifics() -> None:
    u = unit("atlas-egress")
    assert "atlas-audit.socket" in u.words("Requires", "Unit")  # no audit trail, no egress
    assert u.one("ExecStart") == "/var/lib/atlas/current/venv/atlas-egress/bin/python -m atlas.egress"
    assert u.words("SupplementaryGroups") == {"atlas-audit-clients"}


# --- socket units --------------------------------------------------------------------------------------

MULTI_CONSUMER_GROUPS = {p: g for p, g in md_socket_groups().items() if g.endswith("-clients")}


def md_audit_writers() -> set[str]:
    return set(PI_SERVICES) - NON_AUDIT


def md_core_clients() -> set[str]:
    text = section("## 2. Listeners and ports", "**CONNECT proxy ports**")
    row = next(ln for ln in text.splitlines() if "/run/atlas/core.sock" in ln)
    return set(re.findall(r"`(atlas-[a-z]+)`", row.split("|")[3]))


def test_socket_units_match_services_md() -> None:
    sockets = md_sockets()
    assert set(sockets) == {
        "/run/atlas/core.sock",
        "/run/atlas/executor.sock",
        "/run/atlas/audit.sock",
        "/run/atlas/egress-research.sock",
        "/run/atlas/fallback.sock",
    }
    files = {p.stem: Unit(p) for p in SYSTEMD.glob("*.socket")}
    assert len(files) == len(sockets)
    for path, (owner, single_consumer) in sockets.items():
        stem = "atlas-" + Path(path).stem
        u = files[stem]
        assert u.one("ListenStream", "Socket") == path
        assert u.one("SocketMode", "Socket") == "0660"
        assert u.one("SocketUser", "Socket") == owner
        group = u.one("SocketGroup", "Socket")
        if single_consumer is not None:
            assert group == next(iter(single_consumer)), f"{path}: group must be the single consumer's"
        else:
            assert group == MULTI_CONSUMER_GROUPS[path]
        assert u.one("Service", "Socket") in (None, f"{owner}.service")


def test_socket_group_membership_is_exactly_the_permitted_clients() -> None:
    units = {n: unit(n) for n in PI_SERVICES}
    in_audit = {n for n, u in units.items() if "atlas-audit-clients" in u.words("SupplementaryGroups")}
    in_core = {n for n, u in units.items() if "atlas-core-clients" in u.words("SupplementaryGroups")}
    assert in_audit == md_audit_writers() - {"atlas-audit"}
    assert in_core == md_core_clients()
    # Group atlas-core guards executor.sock and fallback.sock: nobody but the atlas-core user may be in it.
    assert all("atlas-core" not in u.words("SupplementaryGroups") for u in units.values())


def test_provisioning_creates_the_shared_groups() -> None:
    script = (DEPLOY / "provisioning" / "provision-pi.sh").read_text()
    for group in MULTI_CONSUMER_GROUPS.values():
        assert f"ensure_group {group}" in script


def test_socket_activated_services_require_their_socket() -> None:
    for name in ("atlas-audit", "atlas-core", "atlas-executor", "atlas-fallback"):
        assert f"{name}.socket" in unit(name).words("Requires", "Unit")


# --- nftables ------------------------------------------------------------------------------------------

NFT = (DEPLOY / "nftables" / "atlas.nft").read_text(encoding="utf-8")
NFT_ACTIVE = [ln.strip() for ln in NFT.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def chain(name: str) -> list[str]:
    body = NFT.split(f"chain {name} {{", 1)[1].split("\n    }", 1)[0]
    return [ln.strip() for ln in body.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def test_all_hooks_default_drop() -> None:
    for hook in ("input", "forward", "output"):
        assert f"type filter hook {hook} priority filter; policy drop;" in chain(hook)[:1]


def test_proxy_ports_are_pinned_to_their_caller_and_precede_the_catch_all() -> None:
    out = chain("output")
    for user, port in md_proxy_ports().items():
        accept = f'oifname "lo" tcp dport {port} meta skuid "{user}" accept'
        assert accept in out, accept
        assert (
            out.index(accept)
            < out.index('oifname "lo" tcp dport 3131-3136 counter drop')
            < out.index('oifname "lo" accept')
        )
    assert out.index('oifname "lo" tcp dport 8888 meta skuid "atlas-egress" accept') < out.index(
        'oifname "lo" tcp dport 8888 counter drop'
    )
    assert min(md_proxy_ports().values()) == 3131 and max(md_proxy_ports().values()) == 3136


def test_only_atlas_egress_reaches_the_internet() -> None:
    out = chain("output")
    internet = [ln for ln in out if "dport 443" in ln and "oifname" not in ln and "ip saddr" not in ln]
    assert internet, "no 443 rule at all"
    assert all(ln.startswith('meta skuid "atlas-egress" ') for ln in internet)
    assert any("ip daddr != @nonpublic4" in ln for ln in internet)
    assert any("ip6 daddr != @nonpublic6" in ln for ln in internet)
    # port 80 is Phase 5 research only: present only as a comment
    assert not [ln for ln in NFT_ACTIVE if "dport 80 " in ln and "skuid" in ln]
    assert any(ln.strip().startswith("# meta skuid") and "dport 80" in ln for ln in NFT.splitlines())
    # nothing outside loopback, DNS, NTP, DHCP, WireGuard, WoL and the egress rules may accept new traffic
    for ln in out[1:]:
        if (
            ln.endswith("accept")
            and "ct state" not in ln
            and not ln.startswith(('oifname "lo"', "udp sport", "icmpv6"))
        ):
            assert any(
                tag in ln for tag in ("$NTP_SERVERS", "$LAN_DNS", "$DESKTOP", "$LAN_BROADCAST", 'skuid "atlas-egress"')
            ), ln


def test_dns_is_only_for_atlas_egress_and_root_and_only_to_the_lan_resolver() -> None:
    dns = [ln for ln in chain("output") if "dport 53" in ln and not ln.startswith("oifname")]
    assert dns
    assert all("$LAN_DNS" in ln and ('skuid "atlas-egress"' in ln or 'skuid "root"' in ln) for ln in dns)


def test_ssh_only_from_the_lan() -> None:
    ssh = [ln for ln in chain("input") if "dport 22" in ln]
    assert ssh == ["ip saddr $LAN_CIDR tcp dport 22 accept"]


def test_wireguard_wol_and_tunnel_rules() -> None:
    out = " ".join(chain("output"))
    assert 'oifname "wg0" ip daddr $DESKTOP_WG_IP tcp dport { 1234, 8770 } meta skuid "atlas-core" accept' in out
    assert 'meta skuid "atlas-core" ip daddr { $LAN_BROADCAST, 255.255.255.255 } udp dport { 7, 9 } accept' in out
    assert "udp dport 51820" in " ".join(chain("input"))


def test_nft_skuids_are_known_services() -> None:
    for user in re.findall(r'skuid "([^"]+)"', NFT):
        assert user == "root" or user in PI_SERVICES, user


def test_nft_variables_are_all_defined_in_the_site_example() -> None:
    site = (DEPLOY / "nftables" / "site.nft.example").read_text()
    defined = set(re.findall(r"^define (\w+) =", site, flags=re.M))
    used = set(re.findall(r"\$(\w+)", "\n".join(NFT_ACTIVE)))
    assert used == defined, (used - defined, defined - used)
    assert 'include "/etc/atlas/nftables-site.nft"' in NFT_ACTIVE


def test_nft_nonpublic_sets_match_the_proxys_address_policy() -> None:
    def elements(name: str) -> set[str]:
        body = NFT.split(f"set {name} {{", 1)[1].split("\n    }", 1)[0]
        listed = body.split("elements = {", 1)[1].split("}", 1)[0]
        return {e.strip() for e in listed.replace("\n", " ").split(",") if e.strip()}

    assert elements("nonpublic4") == {str(n) for n in _FORBIDDEN_V4}
    # the kernel set is the subset of the proxy's IPv6 table that sits inside 2000::/3; the rest is not routable there anyway
    v6_in_global = {str(n) for n in _FORBIDDEN_V6 if n.subnet_of(ipaddress.ip_network("2000::/3"))}
    assert elements("nonpublic6") == v6_in_global


# The one deliberate exception: time.google.com's published anycast addresses, the default NTP_SERVERS (PLAN 5.7), and
# only in the site example.
PUBLIC_TIME_SERVERS = {"216.239.35.0", "216.239.35.4", "216.239.35.8", "216.239.35.12"}


def test_every_ip_literal_under_deploy_is_a_documentation_or_private_address() -> None:
    """No real addresses anywhere in deploy/: only RFC 5737 / RFC 1918 / loopback / link-local style values."""
    pattern = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
    for path in DEPLOY.rglob("*"):
        if not path.is_file() or path.suffix in (".png",):
            continue
        for match in pattern.findall(path.read_text(encoding="utf-8", errors="ignore")):
            if path == DEPLOY / "nftables" / "site.nft.example" and match in PUBLIC_TIME_SERVERS:
                continue
            ip = ipaddress.ip_address(match)
            assert not is_public_address(ip), f"{path.relative_to(REPO)}: {match} looks like a real public address"


# --- provisioning scripts ---------------------------------------------------------------------------------

SCRIPTS = sorted((DEPLOY / "provisioning").glob("*.sh"))


def test_scripts_exist() -> None:
    assert {p.name for p in SCRIPTS} >= {
        "lib.sh", "provision-pi.sh", "deploy.sh", "atlas-release.sh", "store-credential.sh", "gen-audit-keys.sh",
        "pt1-egress-check.sh", "exposure-inventory.sh", "fi3-audit-load.sh", "atlas-backup-mount.sh",
        "gen-backup-key.sh",
    }  # fmt: skip


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_script_hygiene(script: Path) -> None:
    text = script.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash\n")
    if script.name != "lib.sh":  # sourced, not executed
        assert "\nset -euo pipefail\n" in text
        assert script.stat().st_mode & 0o111, "must be executable"
    assert shutil.which("bash")
    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, check=False)  # noqa: S603, S607
    assert result.returncode == 0, result.stderr
    assert not re.search(r"(?i)\b(password|passphrase)\s*=\s*['\"]?\w{6,}", text.replace("passphrase", ""))
    for host in re.findall(r"\b[a-z0-9-]+(?:\.[a-z0-9-]+)+\b", text):
        if host.endswith((".org", ".com", ".net")) and not host.endswith(("example.org", "example.com", "example.net")):
            allowed = {
                "gmail.googleapis.com",
                "discord.com",
            }  # allowlist hosts the PT-1 script probes through the proxy
            assert host in allowed, f"{script.name}: unexpected hostname {host}"


def test_shellcheck_if_available() -> None:
    exe = shutil.which("shellcheck")
    if exe is None:
        pytest.skip("shellcheck is not installed on this machine; run it in the ARM64 VM")
    # -x follows the sourced lib.sh (known issue S1); SCRIPTDIR resolves its `source=lib.sh` directive from any cwd
    argv = [exe, "-x", "--source-path=SCRIPTDIR", *map(str, SCRIPTS)]
    result = subprocess.run(argv, capture_output=True, text=True, check=False)  # noqa: S603
    assert result.returncode == 0, result.stdout


def run_script(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    base = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
    return subprocess.run(  # noqa: S603  (fixed argv, our own scripts)
        ["/bin/bash", *args], capture_output=True, text=True, check=False, env={**base, **(env or {})}, cwd=REPO
    )


def test_provision_dry_run_changes_nothing_and_lists_every_service() -> None:
    result = run_script(str(DEPLOY / "provisioning" / "provision-pi.sh"), "--dry-run")
    assert result.returncode == 0, result.stderr
    for name in PI_SERVICES:
        assert "useradd" in result.stderr and name in result.stderr
    assert "groupadd --system atlas-audit-clients" in result.stderr
    assert "systemctl enable atlas-audit.service atlas-egress.service atlas-audit.socket" in result.stderr
    assert "nft -c -f /etc/atlas/atlas.nft" in result.stderr  # the ruleset is validated before it is enabled
    assert "dry-run" in result.stderr


def test_provision_refuses_to_run_for_real_without_root() -> None:
    if os.getuid() == 0:
        pytest.skip("running as root")
    result = run_script(str(DEPLOY / "provisioning" / "provision-pi.sh"))
    assert result.returncode != 0
    assert "must run as root" in result.stderr


def test_deploy_script_refuses_placeholders_and_missing_settings() -> None:
    script = str(DEPLOY / "provisioning" / "deploy.sh")
    assert run_script(script).returncode != 0
    placeholder = run_script(script, env={"PI_HOST": "atlas-pi.example.org", "PI_USER": "admin"})
    assert placeholder.returncode != 0
    assert "placeholder" in placeholder.stderr


def test_atlas_release_requires_root_and_validates_arguments() -> None:
    if os.getuid() == 0:
        pytest.skip("running as root")
    result = run_script(str(DEPLOY / "provisioning" / "atlas-release.sh"), "activate", "../../etc")
    assert result.returncode != 0
    text = (DEPLOY / "provisioning" / "atlas-release.sh").read_text()
    assert "VERSION_RE='^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{7,40}$'" in text
    assert text.count("valid_version") >= 3  # install, activate, and the rollback target


def test_release_layout_constants_match_services_md() -> None:
    text = (DEPLOY / "provisioning" / "atlas-release.sh").read_text()
    assert "KEEP=3" in text  # "Keep the last 3 releases"
    assert "/var/lib/atlas" in text and "releases" in text and "current" in text
    assert "`releases`: the versioned releases" in SERVICES_MD
    assert "atlas-releases" not in "".join(PI_SERVICES) and "atlas-current" not in PI_SERVICES


def test_socket_group_table_and_reserved_names_were_parsed() -> None:
    groups = md_socket_groups()
    assert groups["/run/atlas/audit.sock"] == "atlas-audit-clients"
    assert groups["/run/atlas/core.sock"] == "atlas-core-clients"
    assert set(MULTI_CONSUMER_GROUPS) == {"/run/atlas/audit.sock", "/run/atlas/core.sock"}
    # every socket unit uses exactly the group the table names
    for path, group in groups.items():
        stem = "atlas-" + Path(path).stem
        assert Unit(SYSTEMD / f"{stem}.socket").one("SocketGroup", "Socket") == group
    assert md_reserved_names() == {"releases", "current", "previous", "incoming", "credentials", "logs"}
    assert not md_reserved_names() & {n.removeprefix("atlas-") for n in PI_SERVICES}
    release = (DEPLOY / "provisioning" / "atlas-release.sh").read_text()
    users = release + "".join((DEPLOY / "provisioning" / f).read_text() for f in ("provision-pi.sh", "base-os.sh"))
    for name in md_reserved_names():
        assert f"/{name}" in users or f"{name}/" in users


def test_services_md_family_table_was_parsed() -> None:
    assert FAMILIES["atlas-egress"] == ({"AF_INET", "AF_INET6"}, "`any`")
    assert FAMILIES["atlas-kiosk"][0] == {"AF_INET"}
    assert FAMILIES["atlas-core"][0] == {"AF_INET"}
    assert set(FAMILIES) == {"atlas-egress", "atlas-ui", "atlas-core", "atlas-kiosk"} | set(md_proxy_ports())


def _apple_double(tar_path: Path) -> list[str]:
    # macOS `tar -t` hides AppleDouble entries when listing; tarfile shows what is really in the archive.
    with tarfile.open(tar_path) as tf:
        return [m.name for m in tf.getmembers() if Path(m.name).name.startswith("._")]


def test_deploy_tarball_carries_no_macos_metadata(tmp_path: Path) -> None:
    """macOS tar adds AppleDouble ``._*`` entries, which atlas-release refuses (found in the ARM64 VM, 2026-11-20)."""
    text = (DEPLOY / "provisioning" / "deploy.sh").read_text(encoding="utf-8")
    assert "COPYFILE_DISABLE=1 tar" in text and "--no-mac-metadata" in text and "--no-xattrs" in text
    tar, xattr = shutil.which("tar"), shutil.which("xattr")
    if tar is None or xattr is None:
        pytest.skip("needs macOS bsdtar and xattr")
    version = subprocess.run([tar, "--version"], capture_output=True, text=True, check=False).stdout  # noqa: S603
    if "bsdtar" not in version:
        pytest.skip("needs macOS bsdtar and xattr")
    stage = tmp_path / "stage"
    (stage / "backend").mkdir(parents=True)
    (stage / "backend" / "x.py").write_text("x = 1\n", encoding="utf-8")
    for target in (stage, stage / "backend" / "x.py"):
        subprocess.run([xattr, "-w", "com.apple.metadata:atlas-test", "1", str(target)], check=True)  # noqa: S603
    env = {k: v for k, v in os.environ.items() if k != "COPYFILE_DISABLE"}
    old, new = tmp_path / "old.tar", tmp_path / "new.tar"
    subprocess.run([tar, "-cf", str(old), "-C", str(stage), "."], check=True, env=env)  # noqa: S603
    if not _apple_double(old):
        pytest.skip("this macOS tar does not write AppleDouble entries here: nothing to prove")
    env["COPYFILE_DISABLE"] = "1"
    fixed = [tar, "--no-xattrs", "--no-mac-metadata", "-cf", str(new), "-C", str(stage), "."]
    subprocess.run(fixed, check=True, env=env)  # noqa: S603
    assert _apple_double(new) == []
