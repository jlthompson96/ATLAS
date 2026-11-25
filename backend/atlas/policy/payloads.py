"""Action payloads: the exact objects a human approves and atlas-executor runs.

events.v1 leaves kind-specific payload shapes to later minor revisions; these shapes match the mock
event server's scenarios (``backend/atlas/ui_server/mock/scenarios/approvals.json``) so the HUD
already renders them. Both atlas-core (before proposing) and atlas-executor (before acting) validate
with ``validate_payload``; the executor never trusts that core did.

Payloads contain no floats (JCS stays unambiguous) and ``payload_canonical`` is the RFC 8785 form.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any, Final

from atlas.audit.jcs import canonicalize
from atlas.core.schema import strict_json_loads
from atlas.core.taint import valid_resource_id
from atlas.policy.types import ActionKind

MAX_CANONICAL_CHARS: Final = 65_536  # events.v1 action.pending.payload_canonical maxLength
MAX_RECIPIENTS: Final = 10
MAX_SUBJECT: Final = 200
MAX_EMAIL_BODY: Final = 20_000
MAX_DISCORD_CONTENT: Final = 2_000  # Discord's own message limit
MIN_SCHEDULE_MINUTES: Final = 60  # RT-12: nothing more often than hourly, even with approval
MAX_SCHEDULE_MINUTES: Final = 7 * 24 * 60
MAX_QUESTION: Final = 300

# Kinds the planner may propose. portfolio.import comes from the HUD's file import, never the planner.
PROPOSABLE_KINDS: Final = frozenset({ActionKind.EMAIL_SEND, ActionKind.DISCORD_POST, ActionKind.RESEARCH_SCHEDULE})
# Kinds atlas-executor runs. research.schedule is applied by atlas-core's scheduler (Phase 5).
EXECUTOR_KINDS: Final = frozenset({ActionKind.EMAIL_SEND, ActionKind.DISCORD_POST})

# Deliberately strict: ASCII local part and domain, at least one dot in the domain, no quoting,
# no comments, no IP literals. Anything unusual is refused rather than interpreted.
EMAIL_RE: Final = re.compile(
    r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
    r"@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}"
)
DISCORD_ID_RE: Final = re.compile(r"[0-9]{17,20}")


def normalize_address(address: str) -> str:
    """Comparison form for allowlists: the domain is case-insensitive; we fold the whole address."""
    return address.strip().casefold()


def valid_email(address: Any) -> bool:
    return isinstance(address, str) and len(address) <= 254 and EMAIL_RE.fullmatch(address) is not None


# Display name: a quoted string, or plain text, without '@' (a second address), ',' ';' ':' (lists, groups),
# '<' '>' (more mailboxes) or control characters (CR, LF, NUL...). Then exactly one <address>, at the end.
_MAILBOX: Final = re.compile(r'(?:"[^"\\<>@\x00-\x1f\x7f]*"|[^"<>@,;:\x00-\x1f\x7f]*)\s*<([^<>\s]{3,254})>')


def mailbox_address(raw: str) -> str | None:
    """The address in a mailbox as an email header gives it: ``addr`` or ``Display Name <addr>``.

    Strict like ``EMAIL_RE``: a display name with no other address, list or group syntax and no control
    characters, then exactly one ``<...>`` at the end holding a valid address; anything else is ``None``.
    The display name is attacker-controlled and dropped; approval shows the bare address (RT-14).
    """
    text = raw.strip()
    if valid_email(text):
        return text
    match = _MAILBOX.fullmatch(text)
    if match is None or not valid_email(match.group(1)):
        return None
    return match.group(1)


def _invisible(ch: str) -> bool:
    """Control (Cc) and format (Cf) characters other than newline and tab: ESC, bidi overrides, zero-width."""
    return ch not in "\n\t" and unicodedata.category(ch) in ("Cc", "Cf")


def display_clean(text: str) -> str:
    """What atlas-core puts in a payload (review F9): CR/LF normalized, invisible characters removed.

    Removing rather than flagging means the payload a human approves is exactly what they can see, and
    no new events.v1 warning is needed. The cost: a zero-width joiner inside an emoji sequence, or a bidi
    mark in right-to-left text, is dropped too. ``validate_payload`` refuses anything not cleaned, so
    the executor never sends invisible characters either.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "".join(ch for ch in text if not _invisible(ch))


def _text(value: Any, max_len: int, *, min_len: int = 0, single_line: bool = False) -> bool:
    if not isinstance(value, str) or not min_len <= len(value) <= max_len:
        return False
    if any(_invisible(ch) for ch in value):
        return False
    return not (single_line and "\n" in value)


def _exact_keys(payload: dict[str, Any], required: set[str], optional: set[str]) -> list[str]:
    keys = set(payload)
    errors = [f"missing {k}" for k in sorted(required - keys)]
    errors += ["unknown key" for _ in sorted(keys - required - optional)]
    return errors


def _email_errors(p: dict[str, Any]) -> list[str]:
    errors = _exact_keys(p, {"kind", "to", "cc", "subject", "body"}, {"in_reply_to"})
    if errors:
        return errors
    for name in ("to", "cc"):
        value = p[name]
        if not isinstance(value, list) or len(value) > MAX_RECIPIENTS:
            errors.append(f"{name}: must be a list of at most {MAX_RECIPIENTS}")
        elif not all(valid_email(a) for a in value):
            errors.append(f"{name}: invalid address")
    if isinstance(p["to"], list) and not p["to"]:
        errors.append("to: at least one recipient")
    if isinstance(p["to"], list) and isinstance(p["cc"], list):
        all_addrs = [normalize_address(a) for a in p["to"] + p["cc"] if isinstance(a, str)]
        if len(set(all_addrs)) != len(all_addrs):
            errors.append("duplicate recipient")
    if not _text(p["subject"], MAX_SUBJECT, single_line=True):
        errors.append("subject: invalid")
    if not _text(p["body"], MAX_EMAIL_BODY, min_len=1):
        errors.append("body: invalid")
    if "in_reply_to" in p and not valid_resource_id(p["in_reply_to"]):
        errors.append("in_reply_to: invalid")
    return errors


def _discord_errors(p: dict[str, Any]) -> list[str]:
    errors = _exact_keys(p, {"kind", "channel_id", "channel_label", "content"}, set())
    if errors:
        return errors
    if not isinstance(p["channel_id"], str) or DISCORD_ID_RE.fullmatch(p["channel_id"]) is None:
        errors.append("channel_id: invalid")
    if not _text(p["channel_label"], 100, min_len=1, single_line=True):
        errors.append("channel_label: invalid")
    if not _text(p["content"], MAX_DISCORD_CONTENT, min_len=1):
        errors.append("content: invalid")
    return errors


def _schedule_errors(p: dict[str, Any]) -> list[str]:
    errors = _exact_keys(p, {"kind", "question", "every_minutes"}, set())
    if errors:
        return errors
    if not _text(p["question"], MAX_QUESTION, min_len=1, single_line=True):
        errors.append("question: invalid")
    every = p["every_minutes"]
    if isinstance(every, bool) or not isinstance(every, int):
        errors.append("every_minutes: must be an integer")
    elif not MIN_SCHEDULE_MINUTES <= every <= MAX_SCHEDULE_MINUTES:
        errors.append("every_minutes: out of range")
    return errors


_VALIDATORS: Final = {
    ActionKind.EMAIL_SEND.value: _email_errors,
    ActionKind.DISCORD_POST.value: _discord_errors,
    ActionKind.RESEARCH_SCHEDULE.value: _schedule_errors,
}


def validate_payload(payload: Any) -> list[str]:
    """Every problem with ``payload`` (empty list: valid). Unknown kinds are invalid."""
    if not isinstance(payload, dict):
        return ["payload: must be an object"]
    validator = _VALIDATORS.get(payload.get("kind"))  # type: ignore[arg-type]
    if validator is None:
        return ["payload: unsupported kind"]
    errors = validator(payload)
    if not errors:
        try:
            if len(canonical_text(payload)) > MAX_CANONICAL_CHARS:
                errors.append("payload: too large")
        except (TypeError, ValueError):
            errors.append("payload: not canonicalizable")
    return errors


def canonical_text(payload: dict[str, Any]) -> str:
    """``payload_canonical``: the JCS serialization as a string."""
    return canonicalize(payload).decode("utf-8")


def payload_sha256(canonical: str) -> str:
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse_canonical(canonical: str) -> dict[str, Any]:
    """Parse ``payload_canonical`` and insist it is exactly canonical (no alternative encodings)."""
    value = strict_json_loads(canonical, max_bytes=MAX_CANONICAL_CHARS * 4, max_depth=4)
    if not isinstance(value, dict):
        raise ValueError("payload is not an object")
    if canonical_text(value) != canonical:
        raise ValueError("payload is not in canonical form")
    return value


def recipients(payload: dict[str, Any]) -> list[str]:
    if payload.get("kind") == ActionKind.EMAIL_SEND.value:
        return [str(a) for a in [*payload.get("to", []), *payload.get("cc", [])]]
    return []
