"""What atlas-ui holds between atlas-core and the browsers: the shared (unredacted) view of core's state.

Fed by the core control socket's snapshot and event pushes. Per-device things (privacy mode, the device
itself, outgoing redaction) live in ``atlas.ui_server.session``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Final

Json = dict[str, Any]

#: Shown until core reports otherwise: every source is "unknown/unavailable" (valid events.v1 HostStatus).
DEFAULT_HOST: Final[Json] = {
    "pi": {"cpu_pct": 0, "ram_pct": 0, "temp_c": 0, "disk_write_mb_24h": 0},
    "llm": {"power": "unknown", "link": "down", "service": "unknown"},
    "voice": {"status": "unavailable", "wake_word": "off"},
}
CORE_OFFLINE_STATE: Final[Json] = {"state": "llm_unreachable", "code": "CORE_UNREACHABLE"}

DEFAULT_LAYOUTS: Final[Json] = {
    "kiosk": {
        "top": "clock",
        "left": ["system", "email", "discord", "calendar"],
        "right": ["pending", "research", "egress_audit", "markets"],
        "bottom": "transcript",
    },
    "remote": {
        "top": "clock",
        "left": ["system", "weather", "email", "discord", "calendar"],
        "right": ["pending", "research", "markets", "portfolio", "egress_audit"],
        "bottom": "transcript",
    },
    "compact": {"left": ["pending", "research", "system", "portfolio"], "right": [], "bottom": "transcript"},
}

_DROP_ON: Final = frozenset({"denied", "expired", "executed", "failed", "withdrawn"})


def _copy(value: Json) -> Json:
    out: Json = json.loads(json.dumps(value))
    return out


@dataclass
class Model:
    state: Json = field(default_factory=lambda: dict(CORE_OFFLINE_STATE))
    host: Json = field(default_factory=lambda: _copy(DEFAULT_HOST))
    panels: dict[str, Json] = field(default_factory=dict)
    actions: dict[str, Json] = field(default_factory=dict)
    tasks: dict[str, Json] = field(default_factory=dict)
    alerts: list[Json] = field(default_factory=list)
    transcript: list[Json] = field(default_factory=list)
    layouts: Json = field(default_factory=lambda: _copy(DEFAULT_LAYOUTS))

    def apply(self, type_: str, p: Json) -> None:
        if type_ == "state.changed":
            self.state = p
        elif type_ == "host.status":
            self.host = p
        elif type_ == "panel.update":
            self.panels[p["panel_id"]] = p
        elif type_ == "action.pending":
            self.actions[p["action_id"]] = p
        elif type_ == "action.resolved" and p["outcome"] in _DROP_ON:
            self.actions.pop(p["action_id"], None)
        elif type_ == "task.progress":
            self.tasks[p["task_id"]] = p
        elif type_ == "alert":
            self.alerts = [*self.alerts, p][-20:]
        elif type_ == "transcript.final":
            self.transcript = [*self.transcript, p][-50:]

    def load_core_snapshot(self, snap: Json) -> None:
        """Replace everything core owns (layouts stay: they belong to atlas-ui)."""
        self.state = snap["state"]
        self.host = snap["host"] or _copy(DEFAULT_HOST)
        self.panels = {p["panel_id"]: p for p in snap["panels"]}
        self.actions = {a["action_id"]: a for a in snap["actions"]}
        self.tasks = {t["task_id"]: t for t in snap["tasks"]}
        self.alerts = list(snap["alerts"])
        self.transcript = list(snap["transcript"])
