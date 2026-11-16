"""Server-side privacy-mode redaction (PLAN.md §4.6, events.v1 ``PrivacyMode``).

Privacy mode is per device. While it is on, the server sends redacted shapes for personal and
secret data and withholds transcript text, so the data never reaches that device at all. The
HUD's blur/fade is a second layer, not the protection.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

Json = dict[str, Any]

#: Server frame types withheld entirely while privacy mode is on.
WITHHELD_TYPES = frozenset({"transcript.partial", "transcript.final", "assistant.delta"})


def redact_panel(update: Mapping[str, Any], now_ms: int) -> Json:
    """Return the privacy-mode version of a ``panel.update`` payload."""
    panel_id, data = update["panel_id"], update["data"]
    if panel_id in ("weather", "egress_audit", "markets"):
        return dict(update)  # visible in privacy mode
    redacted: Json
    if panel_id == "digest":
        redacted = {}
    elif panel_id in ("email", "discord"):
        redacted = {"unread": data["unread"]}
    elif panel_id == "calendar":
        upcoming = [e["start"] for e in data["events"] if e["start"] >= now_ms]
        redacted = {"next_event_start": min(upcoming) if upcoming else None}
    elif panel_id == "portfolio":
        redacted = {"locked": True} if data["locked"] else {"locked": False, "day_change_pct": data["day_change_pct"]}
    else:
        raise ValueError(f"no redaction rule for panel {panel_id!r}")
    return {**update, "redacted": True, "data": redacted}


def redact_action(action: Mapping[str, Any]) -> Json:
    """Pending actions: count only."""
    return {"action_id": action["action_id"], "expires_at": action["expires_at"], "redacted": True}


def redact_task(task: Mapping[str, Any]) -> Json:
    """Research tasks: titles hidden."""
    out = {k: v for k, v in task.items() if k != "title"}
    out["redacted"] = True
    return out


def filter_outgoing(type_: str, payload: Mapping[str, Any], privacy_on: bool, now_ms: int) -> Json | None:
    """Apply privacy mode to one outgoing frame. ``None`` means do not send it."""
    if not privacy_on:
        return dict(payload)
    if type_ in WITHHELD_TYPES:
        return None
    if type_ == "panel.update":
        return redact_panel(payload, now_ms)
    if type_ == "action.pending":
        return redact_action(payload)
    if type_ == "task.progress":
        return redact_task(payload)
    return dict(payload)
