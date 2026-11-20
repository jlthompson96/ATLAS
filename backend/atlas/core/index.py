"""The item index behind ``read_index``: untrusted items, exposed to the planner as metadata only.

Integration services (Phase 4: atlas-google, atlas-discord) deliver ``UntrustedContent`` items here.
``read_index`` shows the planner, per item: an opaque ``$EMAIL_n``/``$DISCORD_n``/``$EVENT_n``
reference, the source kind and resource ID, received time, size and unread flag (all produced by
ATLAS code), and the sender and subject as tainted ``$VAR`` references.

One exception, from trusted configuration: if the sender address exactly matches a contact the user
configured, the planner sees that contact's name (a choice from the user's own list). It still means
"claims to be from", since the From header is attacker-controlled.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Final

from atlas.core.taint import SourceKind, UntrustedContent
from atlas.policy.payloads import normalize_address, valid_email

_ANGLE: Final = re.compile(r"<([^<>\s]{3,254})>\s*$")


class ItemIndex:
    MAX_ITEMS: Final = 2000

    def __init__(self, contacts: Mapping[str, str] | None = None) -> None:
        self._contacts = {normalize_address(a): name for a, name in (contacts or {}).items()}
        self._items: dict[str, UntrustedContent] = {}

    def add(self, item: UntrustedContent) -> None:
        if not isinstance(item, UntrustedContent):
            raise TypeError("the index holds UntrustedContent items")
        if len(self._items) >= self.MAX_ITEMS and item.source.resource_id not in self._items:
            oldest = min(self._items.values(), key=lambda i: i.source.observed_at_ms)
            del self._items[oldest.source.resource_id]
        self._items[item.source.resource_id] = item

    def list(self, source: SourceKind | None, *, limit: int, unread_only: bool) -> list[UntrustedContent]:
        items = [
            i
            for i in self._items.values()
            if (source is None or i.source.kind is source) and (i.unread or not unread_only)
        ]
        items.sort(key=lambda i: (i.source.observed_at_ms, i.source.resource_id), reverse=True)
        return items[:limit]

    def remove(self, resource_id: str) -> None:
        self._items.pop(resource_id, None)

    def resource_ids(self) -> frozenset[str]:
        return frozenset(self._items)

    def contact_name(self, item: UntrustedContent) -> str | None:
        """The configured contact name if the claimed sender address matches one exactly."""
        if item.sender is None or not self._contacts:
            return None
        raw = item.sender._reveal().strip()
        match = _ANGLE.search(raw)
        address = match.group(1) if match else raw
        if not valid_email(address):
            return None
        return self._contacts.get(normalize_address(address))
