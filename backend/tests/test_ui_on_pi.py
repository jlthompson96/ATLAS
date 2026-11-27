"""atlas-ui and atlas-core on the Pi: units, key scripts, provisioning helpers and the HUD in the release.

The scripts run as root on the Pi (and end to end in an ARM64 VM clone); here their text and the parts that run
without root are pinned.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from atlas.core.service import EXECUTOR_SOCKET, STATE_DIR, parser
from atlas.llm.lmstudio import CredentialError, read_token_file
from atlas.ui_server.config import ConfigError, UiConfig, load_config

REPO = Path(__file__).resolve().parents[2]
PROV = REPO / "deploy" / "provisioning"
SYSTEMD = REPO / "deploy" / "systemd"


def text(name: str) -> str:
    return (PROV / name).read_text(encoding="utf-8")


def bash(snippet: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  (our own lib.sh)
        ["/bin/bash", "-c", f'. "{PROV}/lib.sh"; {snippet}', "bash", *args],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": "/usr/bin:/bin"},
    )


# --- units match the code's defaults ----------------------------------------------------------------------------


def test_ui_unit_starts_the_server_with_its_certificate_and_addresses() -> None:
    unit = (SYSTEMD / "atlas-ui.service").read_text()
    assert (
        "ExecStart=/var/lib/atlas/current/venv/atlas-ui/bin/python -m atlas.ui_server serve "
        "--tls-cert /etc/atlas/tls/atlas-ui.crt \\\n"
        "    --host 127.0.0.1 --host ${ATLAS_UI_LAN_IP} --origin https://${ATLAS_UI_LAN_IP}\n"
    ) in unit
    assert "After=network-online.target" in unit and "Wants=network-online.target" in unit
    assert "TEMPLATE" not in unit
    # the code's defaults are the unit's directories
    assert "StateDirectory=atlas/ui\n" in unit
    defaults = UiConfig(443, Path("c"), Path("k"), b"", "")
    assert defaults.state_dir == Path("/var/lib/atlas/ui")
    assert defaults.static_dir == Path("/var/lib/atlas/current/hud")
    assert defaults.core_socket == Path("/run/atlas/core.sock")


def _blank_token() -> Path:
    import tempfile

    path = Path(tempfile.mkdtemp()) / "lmstudio-api-token"
    path.write_text("\n")  # what systemd writes for SetCredential=lmstudio-api-token:\n
    return path


def test_core_unit_matches_the_service_defaults() -> None:
    unit = (SYSTEMD / "atlas-core.service").read_text()
    # No link drop-in (or an empty value): $ATLAS_CORE_ARGS expands to no arguments, i.e. --backend none.
    assert "EnvironmentFile" not in unit and "Environment=ATLAS_CORE_ARGS" not in unit
    assert "ExecStart=/var/lib/atlas/current/venv/atlas-core/bin/python -m atlas.core $ATLAS_CORE_ARGS\n" in unit
    assert parser().parse_args([]).backend == "none"
    assert "StateDirectory=atlas/core\n" in unit and Path("/var/lib/atlas/core") == STATE_DIR
    assert Path("/run/atlas/executor.sock") == EXECUTOR_SOCKET
    assert "SetCredential=lmstudio-api-token:\\n\n" in unit  # a blank default, never a value
    with pytest.raises(CredentialError, match="empty"):  # what a linked core does with the blank default
        read_token_file(_blank_token())


# --- provisioning helpers ---------------------------------------------------------------------------------------


def test_missing_credentials_skips_only_credentials_with_a_default(tmp_path: Path) -> None:
    present = tmp_path / "present.cred"
    present.write_text("x")
    unit = tmp_path / "u.service"
    unit.write_text(
        f"LoadCredentialEncrypted=a:{present}\n"
        f"LoadCredentialEncrypted=b:{tmp_path}/b.cred\n"
        f"LoadCredentialEncrypted=c:{tmp_path}/c.cred\n"
        "SetCredential=c:\\n\n"
        "SetCredential=bb:\\n\n"  # a default for another name doesn't make b optional
    )
    r = bash('missing_credentials "$1"', str(unit))
    assert r.returncode == 0 and r.stdout == f"{tmp_path}/b.cred\n"


def _core_link(tmp_path: Path, content: str) -> tuple[str, str]:
    """(problem, drop-in) for a core-link.conf holding ``content``."""
    conf = tmp_path / "core-link.conf"
    conf.write_text(content)
    problem = bash('core_link_problem "$1"', str(conf))
    dropin = bash('core_link_dropin "$1"', str(conf))
    assert problem.returncode == 0 and problem.stderr == "", problem.stderr
    return problem.stdout, dropin.stdout


def _dropin_args(dropin: str) -> list[str]:
    line = dropin.splitlines()[-1]
    assert line.startswith('Environment="ATLAS_CORE_ARGS=') and line.endswith('"'), line
    return line[len('Environment="ATLAS_CORE_ARGS=') : -1].split()  # systemd splits $ATLAS_CORE_ARGS at whitespace


def test_core_link_template_is_inert_and_its_examples_parse(tmp_path: Path) -> None:
    template = bash("core_link_conf_template").stdout
    assert all(line.startswith("#") for line in template.splitlines())
    problem, dropin = _core_link(tmp_path, template)
    assert problem == "" and _dropin_args(dropin) == []  # unlinked until the owner edits the file
    assert dropin.splitlines()[1] == "[Service]"
    examples = [line[2:] for line in template.splitlines() if line.startswith("# ATLAS_CORE_ARGS=")]
    assert len(examples) == 2
    for example in examples:
        problem, dropin = _core_link(tmp_path, example + "\n")
        assert problem == ""
        args = parser().parse_args(_dropin_args(dropin))
        assert (args.backend, args.model, args.reasoning_effort) == ("lmstudio", "google/gemma-4-e4b", "none")
        assert args.base_url == "http://10.77.0.2:1234" and args.hostd_url == "http://10.77.0.2:8770"
        assert args.token_file is None and args.socket is None and args.credentials_dir is None
    assert parser().parse_args(_dropin_args(_core_link(tmp_path, examples[0])[1])).no_hostd


def test_core_link_allows_only_the_link_flags(tmp_path: Path) -> None:
    flags = bash('printf "%s\\n" "${CORE_LINK_FLAGS[@]}"').stdout.split()
    known = {o for a in parser()._actions for o in a.option_strings}
    assert set(flags) <= known and "--base-url" not in flags and "--token-file" not in flags
    ok = {
        "": [],
        "# comment only\n": [],
        'ATLAS_CORE_ARGS=""\n': [],
        "ATLAS_CORE_ARGS=--backend lmstudio   --model=m\n": ["--backend", "lmstudio", "--model=m"],
        'ATLAS_CORE_ARGS="--model * --idle-sleep"': ["--model", "*", "--idle-sleep"],  # no glob expansion
    }
    for content, words in ok.items():
        problem, dropin = _core_link(tmp_path, content)
        assert problem == "" and _dropin_args(dropin) == words, content
    bad = {
        'ATLAS_CORE_ARGS="--backend lmstudio --base-url http://192.0.2.9:1234"': "--base-url is not an allowed flag",
        'ATLAS_CORE_ARGS="--token-file=/tmp/t"': "--token-file is not an allowed flag",
        'ATLAS_CORE_ARGS="--socket /tmp/s"': "--socket is not",
        'ATLAS_CORE_ARGS="--credentials-dir /tmp"': "--credentials-dir is not",
        'ATLAS_CORE_ARGS="--model $(id)"': "only comments and one",
        'ATLAS_CORE_ARGS="--model `id`"': "only comments and one",
        'ATLAS_CORE_ARGS="--model \\x"': "only comments and one",
        'ATLAS_CORE_ARGS="--model %h"': "only comments and one",  # a systemd specifier in Environment=
        'ATLAS_CORE_ARGS="--model m" --socket /tmp/s': "only comments and one",
        "ATLAS_CORE_ARGS='--backend lmstudio'": "only comments and one",
        "OTHER=1": "only comments and one",
        'ATLAS_CORE_ARGS="--backend none"\nATLAS_CORE_ARGS="--backend lmstudio"': "ATLAS_CORE_ARGS is set more than once",
    }
    for content, why in bad.items():
        problem, _ = _core_link(tmp_path, content + "\n")
        assert problem.startswith(why) and problem.count("\n") == 1, (content, problem)


def test_provision_checks_the_core_link_file_before_writing_the_dropin() -> None:
    t = text("provision-pi.sh")
    create = t.split('if [ -e "$CORE_LINK_CONF" ]; then', 1)[1].split("\nfi\n", 1)[0]
    assert "never overwritten" in create and 'write_file "$CORE_LINK_CONF" 644 "$(core_link_conf_template)"' in create
    after = t.split('if [ -e "$CORE_LINK_CONF" ]; then', 1)[1]
    check = after.index('[ -z "$problem" ] || die "$CORE_LINK_CONF: $problem"')
    assert check < after.index('write_file "$CORE_LINK_DROPIN" 644 "$(core_link_dropin "$link_conf")"')
    assert bash('echo "$CORE_LINK_DROPIN"').stdout == "/etc/systemd/system/atlas-core.service.d/link.conf\n"
    v = (PROV / "pi-verify.sh").read_text()
    assert 'core_link_problem "$CORE_LINK_CONF"' in v and 'core_link_dropin "$CORE_LINK_CONF"' in v


def test_pi_lan_ip_ignores_the_placeholder_and_refuses_junk(tmp_path: Path) -> None:
    site = tmp_path / "site.nft"
    for value, out in (("192.0.2.48", ""), ("10.0.0.48", "10.0.0.48\n")):
        site.write_text(f"define PI_LAN_IP = {value}   # comment\n")
        r = bash('pi_lan_ip "$1"', str(site))
        assert r.returncode == 0 and r.stdout == out, value
    site.write_text("define LAN_CIDR = 10.0.0.0/24\n")
    assert bash('pi_lan_ip "$1"', str(site)).stdout == ""
    site.write_text("define PI_LAN_IP = 10.0.0.48;rm\n")
    r = bash('pi_lan_ip "$1"', str(site))
    assert r.returncode == 1 and "plain IPv4" in r.stderr


def test_provision_enables_only_services_that_can_start() -> None:
    t = text("provision-pi.sh")
    assert 'missing="$(missing_credentials "$deploy_dir/systemd/$name.service"' in t
    assert "Environment=ATLAS_UI_LAN_IP=$ui_ip" in t
    assert 'ensure_dir "$UI_TLS_DIR" 0755 root:root' in t


# --- key scripts ------------------------------------------------------------------------------------------------


def test_gen_ui_keys_keeps_no_ca_key_and_prints_no_secret() -> None:
    t = text("gen-ui-keys.sh")
    assert "mktemp -d -p /run" in t  # keys only in RAM
    assert t.index('rm -f "$tmp/ca.key"') < t.index("store-credential.sh")  # the CA key is gone before anything is kept
    assert "-CAkey" in t and t.count('"$tmp/ca.key"') == 4  # created, self-signed, signs once, deleted
    assert "nameConstraints=critical,permitted;IP:$ip/255.255.255.255" in t
    assert "subjectAltName=IP:$ip,IP:127.0.0.1,DNS:localhost" in t
    assert "extendedKeyUsage=serverAuth" in t
    assert re.search(r"^UI_CERT_DAYS=(\d+)", text("lib.sh"), flags=re.M).group(1) == "800"  # type: ignore[union-attr]
    # never printed: the server key goes to store-credential.sh on stdin, the device key through a pipe
    assert '"$here/store-credential.sh" atlas-ui tls-key <"$tmp/server.key"' in t
    assert 'openssl rand 32 | "$here/store-credential.sh" atlas-ui device-token-key' in t
    for leak in ('cat "$tmp/server.key', 'cat "$tmp/ca.key', "openssl rand -hex 32", "tee"):
        assert leak not in t, leak
    assert "root_is_overlay" in t  # /etc/atlas/tls must survive a reboot


def test_gen_ui_pin_needs_the_console_and_hashes_as_atlas_ui() -> None:
    t = text("gen-ui-pin.sh")
    assert "\nneed_local_console\n" in t
    assert t.count("read -rsp") == 2  # typed twice, never echoed
    # release code runs as atlas-ui in a sandbox, on pipes only: never root, never this terminal
    assert 'hash="$(printf \'%s\\n\' "$pin" | ui_python "$py" -I -c "$hasher" "$release" 2>&1)"' in t
    assert "runuser" not in t
    assert 'printf \'%s\' "$hash" | "$here/store-credential.sh" atlas-ui stepup-pin-hash' in t
    assert 'echo "$pin' not in t and 'echo "$hash' not in t and "logger" not in t and "| tee" not in t
    env = {"PATH": "/usr/bin:/bin", "DRY_RUN": "1"}
    r = subprocess.run(  # noqa: S603
        ["/bin/bash", str(PROV / "gen-ui-pin.sh")],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert r.returncode == 1 and "run this on the Pi's own console" in r.stderr and r.stdout == ""


def test_gen_core_keys_prints_only_the_public_key() -> None:
    t = text("gen-core-keys.sh")
    assert "openssl genpkey -algorithm ed25519" in t and "mktemp -d -p /run" in t
    assert '"$here/store-credential.sh" atlas-core approval-signing-key <"$tmp/key"' in t
    assert 'cat "$pub"' in t and 'cat "$tmp/key"' not in t


def test_key_scripts_are_executable() -> None:
    for name in ("gen-ui-keys.sh", "gen-ui-pin.sh", "gen-core-keys.sh"):
        assert os.access(PROV / name, os.X_OK), name


# --- the HUD in the release ------------------------------------------------------------------------------------


def test_deploy_builds_the_hud_from_head_with_locked_dependencies() -> None:
    t = text("deploy.sh")
    assert 'git archive --format=tar HEAD frontend/hud | tar -x -C "$work"' in t
    assert "npm ci --ignore-scripts" in t
    assert 'cp -R "$work/frontend/hud/dist" "$stage/hud"' in t
    assert "frontend/hud/node_modules" not in t and '"$repo/frontend/hud/dist"' not in t  # never the working tree


def test_release_accepts_hud_and_services_md_documents_it() -> None:
    assert 'ALLOWED_TOP="backend contracts deploy hud wheels"' in text("atlas-release.sh")
    services = (REPO / "contracts" / "services.md").read_text()
    assert "A release holds `backend/`, `contracts/`, `deploy/`, `venv/<service>/` and `hud/`" in services
    assert "keeps **no private key**" in services


def test_ui_devices_runs_sandboxed_on_pipes_and_pair_refuses_tmux() -> None:
    t = text("ui-devices.sh")
    assert 'ui_python -p ReadWritePaths=/var/lib/atlas/ui "$py" -I -u -c "$cli"' in t
    assert "</dev/null 2>&1 | cat" in t and "runuser" not in t
    assert '[ "${args[0]}" = pair ] && in_shared_terminal' in t


def test_ui_python_sandbox_mirrors_the_unit() -> None:
    lib = text("lib.sh")
    body = lib.split("ui_python() {", 1)[1].split("\n}\n", 1)[0]
    assert "--uid=atlas-ui --gid=atlas-ui" in body and "--pipe --wait" in body
    unit = (SYSTEMD / "atlas-ui.service").read_text()
    for prop in ("NoNewPrivileges=yes", "ProtectSystem=strict", "ProtectHome=yes", "PrivateTmp=yes", "PrivateDevices=yes",
                 "MemoryDenyWriteExecute=yes", "SystemCallFilter=@system-service", "ProtectProc=invisible", "UMask=0077",
                 "RestrictNamespaces=yes", "LockPersonality=yes"):  # fmt: skip
        assert f"\n{prop}\n" in unit and f"-p {prop}" in body, prop
    assert "-p PrivateNetwork=yes" in body and "-p CapabilityBoundingSet= " in body


def test_renewal_keeps_the_old_key_until_the_new_one_is_ready() -> None:
    t = text("gen-ui-keys.sh")
    assert (
        t.index("openssl verify -CAfile")
        < t.index('rm -f "$key_cred"')
        < t.index('store-credential.sh" atlas-ui tls-key')
    )
    assert "NTPSynchronized" in t


def test_pi_lan_ip_refuses_addresses_that_are_not_a_lan_host(tmp_path: Path) -> None:
    site = tmp_path / "site.nft"
    for bad in ("0.0.0.0", "127.0.0.1", "169.254.1.2", "255.255.255.255"):  # noqa: S104  (refused values)
        site.write_text(f"define PI_LAN_IP = {bad}\n")
        r = bash('pi_lan_ip "$1"', str(site))
        assert r.returncode == 1 and "Pi's own LAN address" in r.stderr, bad


def test_atlas_ui_refuses_to_listen_on_every_address(tmp_path: Path) -> None:
    for name in ("device-token-key", "stepup-pin-hash"):
        (tmp_path / name).write_text("x")
    for host, message in (("", "must be an IP address"), ("0.0.0.0", "every address"), ("::", "every address"),  # noqa: S104
                          ("atlas.lan", "must be an IP address")):  # fmt: skip
        with pytest.raises(ConfigError, match=message):
            load_config(credentials=tmp_path, tls_cert=tmp_path / "c", hosts=("127.0.0.1", host))
    assert load_config(credentials=tmp_path, tls_cert=tmp_path / "c", hosts=("127.0.0.1", "10.0.0.48")).hosts


def test_deploy_builds_the_hud_in_a_clean_environment() -> None:
    assert 'env -i PATH="$PATH" HOME="$HOME" npm run -s build' in text("deploy.sh")
