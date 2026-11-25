"""atlas-ui: UI-SEC-1..4, pairing, device tokens, static serving and the HTTP surface (PLAN.md section 8, T7)."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import pytest
from websockets.exceptions import ConnectionClosed, InvalidStatus

from atlas.ui_server import app as ui_app
from atlas.ui_server.devices import COOKIE_NAME, DeviceRegistry, PairingStore, parse_cookie
from atlas.ui_server.security import (
    CSP,
    SECURITY_HEADERS,
    FailureLimiter,
    default_origins,
    host_allowed,
    origin_allowed,
    same_origin_request,
)
from tests.ui_support import Stack


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# --- UI-SEC-1: unauthenticated WebSocket rejected -----------------------------------------------------


def test_ui_sec_1_websocket_without_cookie_is_refused() -> None:
    async def go() -> None:
        async with Stack() as s:
            with pytest.raises(InvalidStatus) as exc:
                await s.ws(None, wait_snapshot=False)
            assert exc.value.response.status_code == 401

    run(go())


def test_ui_sec_1_forged_and_garbage_cookies_are_refused() -> None:
    async def go() -> None:
        async with Stack() as s:
            good = await s.pair()
            name, _, value = good.partition("=")
            device_id, _, mac = value.partition(".")
            for bad in (
                f"{name}=garbage",
                f"{name}={device_id}.{'A' * len(mac)}",  # right device, wrong MAC
                f"{name}=dev-000000000000.{mac}",  # unknown device
                f"{name}=",
                f"{name}={device_id}.{mac}.extra",
                "other=" + value,  # wrong cookie name
            ):
                with pytest.raises(InvalidStatus) as exc:
                    await s.ws(bad, wait_snapshot=False)
                assert exc.value.response.status_code == 401, bad

    run(go())


# --- UI-SEC-2: cross-origin rejected ------------------------------------------------------------------


def test_ui_sec_2_cross_origin_websocket_is_refused_even_with_a_valid_cookie() -> None:
    async def go() -> None:
        async with Stack() as s:
            cookie = await s.pair()
            for origin in ("https://evil.example.com", "http://localhost", f"http://localhost:{s.port}", "null", None):
                with pytest.raises(InvalidStatus) as exc:
                    await s.ws(cookie, origin=origin, wait_snapshot=False)
                assert exc.value.response.status_code == 403, origin
            ok = await s.ws(cookie)  # same origin works
            await ok.conn.close()

    run(go())


def test_ui_sec_2_cross_origin_pairing_is_refused() -> None:
    async def go() -> None:
        async with Stack() as s:
            code = s.server.pairing.create("remote")
            reply = await s.http("/api/pair", {"Origin": "https://evil.example.com", "X-Atlas-Pairing-Code": code})
            assert reply.status == 403
            assert "set-cookie" not in reply.headers
            # The code was not spent by the refused request.
            reply = await s.http("/api/pair", {"Origin": s.origin, "X-Atlas-Pairing-Code": code})
            assert reply.status == 200

    run(go())


def test_the_pairing_page_itself_can_pair_without_an_origin() -> None:
    """Browsers send no Origin on a same-origin GET; the pairing page's fetch is one (found with the kiosk in the VM)."""

    async def go() -> None:
        async with Stack() as s:
            for headers in (
                {},  # not a browser page: no Origin, no Fetch Metadata
                {"Sec-Fetch-Site": "cross-site"},  # another site's page (a browser would also send its Origin)
                {"Sec-Fetch-Site": "same-site"},  # a sibling subdomain is not this origin
                {"Sec-Fetch-Site": "none"},  # typed into the address bar, not the page
                {"Origin": "https://evil.example.com", "Sec-Fetch-Site": "same-origin"},  # the Origin decides when sent
            ):
                code = s.server.pairing.create("remote")
                reply = await s.http("/api/pair", {**headers, "X-Atlas-Pairing-Code": code})
                assert reply.status == 403, headers
                assert "set-cookie" not in reply.headers
            code = s.server.pairing.create("kiosk")
            reply = await s.http("/api/pair", {"Sec-Fetch-Site": "same-origin", "X-Atlas-Pairing-Code": code})
            assert reply.status == 200 and reply.headers["set-cookie"].startswith(COOKIE_NAME + "=")

    run(go())


def test_host_header_must_name_this_server() -> None:
    async def go() -> None:
        async with Stack() as s:
            assert (await s.http("/pair", host="evil.example.com")).status == 421
            assert (await s.http("/pair", host=f"localhost:{s.port}")).status == 200

    run(go())


def test_origin_helpers() -> None:
    allowed = default_origins(443, ["https://atlas.lan/"])
    assert origin_allowed("https://atlas.lan", allowed)
    assert origin_allowed("HTTPS://LOCALHOST", allowed)
    assert not origin_allowed("https://atlas.lan.evil.com", allowed)
    assert not origin_allowed(None, allowed) and not origin_allowed("null", allowed)
    assert host_allowed("atlas.lan", allowed) and not host_allowed("atlas.lan:8443", allowed)
    assert same_origin_request(None, "same-origin", allowed) and same_origin_request(None, " Same-Origin ", allowed)
    assert not same_origin_request(None, None, allowed) and not same_origin_request(None, "cross-site", allowed)
    assert not same_origin_request("https://evil.example.com", "same-origin", allowed)
    assert same_origin_request("https://atlas.lan", None, allowed)
    assert not host_allowed(None, allowed)


# --- UI-SEC-3 / 4: CSP and no external requests -----------------------------------------------------


def test_ui_sec_3_security_headers_on_every_response_kind() -> None:
    async def go() -> None:
        async with Stack() as s:
            cookie = await s.pair()
            replies = [
                await s.http("/pair"),
                await s.http("/pair.js"),
                await s.http("/"),  # redirect to /pair
                await s.http("/nope.png"),  # 401
                await s.http("/", {"Cookie": cookie}),
                await s.http("/assets/app.js", {"Cookie": cookie}),
                await s.http("/missing.js", {"Cookie": cookie}),  # 404
                await s.http("/ws/events"),  # not an upgrade
                await s.http("/api/pair", {"Origin": s.origin}),  # missing code
            ]
            assert [r.status for r in replies] == [200, 200, 302, 401, 200, 200, 404, 426, 400]
            for reply in replies:
                for name, value in SECURITY_HEADERS.items():
                    assert reply.headers[name.lower()] == value, (reply.status, name)
                assert "server" not in reply.headers  # no software banner

    run(go())


def test_ui_sec_3_csp_forbids_everything_external() -> None:
    assert CSP.startswith("default-src 'self'")
    for directive in ("script-src 'self'", "style-src 'self'", "connect-src 'self'", "frame-ancestors 'none'"):
        assert directive in CSP
    assert "unsafe-inline" not in CSP and "unsafe-eval" not in CSP and "http" not in CSP and "*" not in CSP


EXTERNAL_URL = re.compile(r"""(?:src|href|action)=["']?(?:https?:)?//""", re.IGNORECASE)


def test_ui_sec_4_served_pages_load_nothing_external() -> None:
    web = Path(ui_app.__file__).parent / "web"
    for name in ("pair.html", "pair.js", "pair.css"):
        text = (web / name).read_text(encoding="utf-8")
        assert not EXTERNAL_URL.search(text), name
        assert "@import" not in text and "url(http" not in text, name
    html = (web / "pair.html").read_text(encoding="utf-8")
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), "inline script would violate the CSP"
    assert "onclick=" not in html and "style=" not in html


def test_built_hud_has_no_external_references_if_built() -> None:
    dist = Path(ui_app.__file__).resolve().parents[3] / "frontend" / "hud" / "dist"
    if not (dist / "index.html").is_file():
        pytest.skip("HUD not built (npm run build in frontend/hud)")
    for path in dist.rglob("*"):
        if path.suffix in {".html", ".css"}:
            text = path.read_text(encoding="utf-8", errors="replace")
            assert not EXTERNAL_URL.search(text), path.name
            assert not re.search(r"url\(\s*[\"']?https?:", text), path.name
    html = (dist / "index.html").read_text(encoding="utf-8")
    assert "Content-Security-Policy" in html


# --- pairing ----------------------------------------------------------------------------------------


def test_pairing_code_is_single_use_and_sets_a_hardened_cookie() -> None:
    async def go() -> None:
        async with Stack() as s:
            code = s.server.pairing.create("remote")
            first = await s.http("/api/pair", {"Origin": s.origin, "X-Atlas-Pairing-Code": code})
            assert first.status == 200
            cookie = first.headers["set-cookie"]
            assert cookie.startswith(COOKIE_NAME + "=") and COOKIE_NAME.startswith("__Host-")
            for attr in ("Secure", "HttpOnly", "SameSite=Strict", "Path=/"):
                assert attr in cookie
            assert "Domain" not in cookie
            second = await s.http("/api/pair", {"Origin": s.origin, "X-Atlas-Pairing-Code": code})
            assert second.status == 401
            # Lower case and a missing dash are accepted for typing convenience; a different code is not.
            fresh = s.server.pairing.create("kiosk")
            relaxed = fresh.replace("-", "").lower()
            assert (await s.http("/api/pair", {"Origin": s.origin, "X-Atlas-Pairing-Code": relaxed})).status == 200

    run(go())


def test_pairing_codes_expire(tmp_path: Path) -> None:
    now = [1_000_000]
    store = PairingStore(tmp_path, wall_ms=lambda: now[0])
    code = store.create("remote", ttl_s=60)
    now[0] += 59_000
    assert store.redeem("WRONG-CODE") is None
    now[0] += 2_000
    assert store.redeem(code) is None  # expired


def test_pairing_codes_are_stored_hashed_and_bounded(tmp_path: Path) -> None:
    store = PairingStore(tmp_path)
    code = store.create("kiosk")
    raw = (tmp_path / "pairing.json").read_text()
    assert code.replace("-", "") not in raw and code not in raw
    assert (tmp_path / "pairing.json").stat().st_mode & 0o777 == 0o600
    for _ in range(7):
        store.create("remote")
    with pytest.raises(OverflowError):
        store.create("remote")
    assert store.redeem(code) == "kiosk"
    assert store.redeem(code) is None


def test_pairing_is_rate_limited_per_address_and_blocks_a_valid_code_while_locked() -> None:
    async def go() -> None:
        async with Stack() as s:
            statuses = []
            for i in range(7):
                reply = await s.http("/api/pair", {"Origin": s.origin, "X-Atlas-Pairing-Code": f"AAAA-{i:04d}"})
                statuses.append(reply.status)
            assert statuses == [401] * 5 + [429] * 2
            good = s.server.pairing.create("remote")
            reply = await s.http("/api/pair", {"Origin": s.origin, "X-Atlas-Pairing-Code": good})
            assert reply.status == 429
            assert "set-cookie" not in reply.headers

    run(go())


def test_failure_limiter_window_and_lockout() -> None:
    now = [0.0]
    lim = FailureLimiter(3, 10, 30, clock=lambda: now[0])
    lim.failure("a")
    now[0] += 11  # the first failure ages out
    lim.failure("a")
    lim.failure("a")
    assert not lim.locked("a")
    lim.failure("a")
    assert lim.locked("a") and not lim.locked("b")
    now[0] += 31
    assert not lim.locked("a")


def test_static_files_need_a_device_and_cannot_escape_the_root() -> None:
    async def go() -> None:
        async with Stack() as s:
            cookie = await s.pair()
            auth = {"Cookie": cookie}
            home = await s.http("/", {"Accept": "text/html"})
            assert home.status == 302 and home.headers["location"] == "/pair"
            assert (await s.http("/assets/app.js")).status == 401
            assert (await s.http("/assets/app.js", auth)).body == b"console.log('hud')"
            assert (await s.http("/assets/app.js", auth)).headers["content-type"].startswith("text/javascript")
            assert b"<title>HUD</title>" in (await s.http("/some/route", auth)).body  # app shell fallback
            for evil in (
                "/../secret.txt",
                "/%2e%2e/secret.txt",
                "/assets/../../secret.txt",
                "/.git/config",
                "//etc/passwd",
            ):
                reply = await s.http(evil, auth)
                assert b"outside the static root" not in reply.body, evil
                assert reply.status in (404, 200), evil
            assert (await s.http("/../secret.txt", auth)).status == 404

    run(go())


# --- device tokens and registry -----------------------------------------------------------------------


def test_device_tokens_are_bound_to_the_key_and_the_registry(tmp_path: Path) -> None:
    reg = DeviceRegistry(tmp_path, b"a" * 32)
    device, token = reg.add("kiosk")
    assert reg.authenticate(token) == device
    assert DeviceRegistry(tmp_path, b"b" * 32).authenticate(token) is None  # different key
    assert DeviceRegistry(tmp_path / "other", b"a" * 32).authenticate(token) is None  # unknown device
    did, mac = token.split(".")
    assert reg.authenticate(f"{did}.{mac[:-1]}A" if mac[-1] != "A" else f"{did}.{mac[:-1]}B") is None
    assert reg.revoke(device.device_id) and not reg.revoke(device.device_id)
    assert reg.authenticate(token) is None
    assert (tmp_path / "devices.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="32 bytes"):
        DeviceRegistry(tmp_path, b"short")


def test_the_role_comes_from_the_registry_not_the_token(tmp_path: Path) -> None:
    reg = DeviceRegistry(tmp_path, b"a" * 32)
    kiosk, token = reg.add("kiosk")
    got = reg.authenticate(token)
    assert got is not None and got.role == "kiosk" and not got.can_approve and not got.can_unlock_portfolio
    remote, token2 = reg.add("remote")
    got2 = reg.authenticate(token2)
    assert got2 is not None and got2.can_approve and remote.device_id != kiosk.device_id


def test_registry_is_bounded(tmp_path: Path) -> None:
    reg = DeviceRegistry(tmp_path, b"a" * 32)
    ids = [reg.add("remote")[0].device_id for _ in range(32)]
    with pytest.raises(OverflowError):
        reg.add("remote")
    reg.revoke(ids[0])
    reg.add("remote")  # a revoked device frees its slot


def test_cookie_parsing() -> None:
    assert parse_cookie("a=1; __Host-x=tok; b=2", "__Host-x") == "tok"
    assert parse_cookie("a=1", "__Host-x") is None
    assert parse_cookie(None, "x") is None
    assert parse_cookie("x=" + "a" * 5000, "x") is None


def test_revoking_a_device_closes_its_open_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ui_app, "TICK_S", 0.05)

    async def go() -> None:
        async with Stack() as s:
            cookie = await s.pair()
            client = await s.ws(cookie)
            device_id = cookie.partition("=")[2].split(".")[0]
            assert s.server.registry.revoke(device_id)
            with pytest.raises(ConnectionClosed):
                async with asyncio.timeout(5):
                    while True:
                        await client.conn.recv()
            with pytest.raises(InvalidStatus):  # and it cannot come back
                await s.ws(cookie, wait_snapshot=False)

    run(go())


def test_connection_cap() -> None:
    async def go() -> None:
        async with Stack(max_connections=1) as s:
            cookie = await s.pair()
            first = await s.ws(cookie)
            with pytest.raises(InvalidStatus) as exc:
                await s.ws(cookie, wait_snapshot=False)
            assert exc.value.response.status_code == 503
            await first.conn.close()

    run(go())
