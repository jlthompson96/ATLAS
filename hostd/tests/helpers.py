"""Test helpers: drive the app without sockets."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from atlas_hostd.http import Request, Response
from atlas_hostd.platform.fake import EPOCH
from atlas_hostd.service import HostdApp, Listener

PI = "10.77.0.1"


async def call(
    app: HostdApp,
    method: str,
    target: str,
    body: Any = None,
    *,
    peer: str = PI,
    listener: Listener = Listener.MAIN,
    content_type: str | None = "application/json",
    raw: bytes | None = None,
) -> Response:
    payload = raw if raw is not None else (b"" if body is None else json.dumps(body).encode())
    ctype = content_type if payload else None
    return await app.handle(listener, Request(method, target, ctype, payload, peer))


def ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def sleep_body(
    request_id: str = "req-1",
    *,
    first_packet_at: datetime | None = EPOCH - timedelta(minutes=31),
    reason: str = "idle",
    atlas_idle_s: int = 1300,
) -> dict[str, Any]:
    wake = None if first_packet_at is None else {"wake_id": "wake-1", "first_packet_at": ts(first_packet_at)}
    return {"request_id": request_id, "reason": reason, "atlas_idle_s": atlas_idle_s, "wake": wake}


def helper_body(*, idle: int = 5000, fullscreen: bool = False, heavy: int = 0, session: int = 1) -> dict[str, Any]:
    return {
        "session_id": session,
        "input_idle_s": idle,
        "fullscreen_app": fullscreen,
        "gpu_heavy_process_count": heavy,
        "reported_at": ts(EPOCH),
    }


async def post_helper(app: HostdApp, **kwargs: Any) -> Response:
    return await call(
        app, "POST", "/v1/helper/activity", helper_body(**kwargs), peer="127.0.0.1", listener=Listener.HELPER
    )
