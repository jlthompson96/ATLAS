"""Hidden-content stripping and size bounds for untrusted items, before the quarantined reader (RT-2, RT-11).

What the reader sees is what a human would see, bounded in size:

* **Size.** Raw input is cut at ``MAX_RAW_BYTES`` before any parsing (bounded CPU), and the visible
  text at ``MAX_VISIBLE_CHARS``. Both set ``TRUNCATED``.
* **HTML.** Only visible text survives. Dropped: ``script``/``style``/``head``/``template``/
  ``noscript``/``iframe``/``object``/``svg`` and similar; comments; elements with the ``hidden``
  attribute, ``aria-hidden="true"`` or ``type=hidden``; inline styles that hide text
  (``display:none``, ``visibility:hidden``, zero font size, zero opacity, white or transparent text,
  zero-size boxes with hidden overflow, off-screen positioning, clipping, big negative indents);
  ``<font color=white>`` and ``<font size=0>``. Hiding through a stylesheet class cannot be resolved
  without a CSS engine, so a ``<style>`` block that contains hiding rules sets ``HIDDEN_HTML`` too.
* **Unicode.** Removed: zero-width characters, bidi controls, Unicode tag characters (U+E0000..E007F,
  used for "ASCII smuggling"), variation selectors, other format (Cf) characters and C0/C1 controls
  other than newline and tab.

Every removal sets a flag. Flags are a closed enum, so they are safe to show the planner.

``Sanitized.text`` is still untrusted. It is used only by ``atlas.core.reader`` to build the reader
prompt (pinned by tests).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from html.parser import HTMLParser
from typing import Final

MAX_RAW_BYTES: Final = 1024 * 1024
MAX_VISIBLE_CHARS: Final = 12_000


class ContentFlag(StrEnum):
    HIDDEN_HTML = "hidden_html"
    ZERO_WIDTH = "zero_width"
    BIDI_CONTROL = "bidi_control"
    UNICODE_TAGS = "unicode_tags"
    VARIATION_SELECTORS = "variation_selectors"
    CONTROL_CHARS = "control_chars"
    INVALID_UTF8 = "invalid_utf8"
    TRUNCATED = "truncated"


@dataclass(frozen=True, slots=True)
class Sanitized:
    text: str
    flags: frozenset[ContentFlag]
    removed_chars: int
    raw_bytes: int


# --- Unicode ---------------------------------------------------------------------------------------

_ZERO_WIDTH: Final = frozenset("​‌‍⁠⁡⁢⁣⁤﻿᠎­͏ᅟᅠㅤﾠ឴឵")
_BIDI: Final = frozenset("‪‫‬‭‮⁦⁧⁨⁩‎‏؜")


def _char_flag(ch: str) -> ContentFlag | None:
    """The flag for a character that must be removed, or None to keep it."""
    cp = ord(ch)
    if ch in ("\n", "\t"):
        return None
    if ch in _ZERO_WIDTH:
        return ContentFlag.ZERO_WIDTH
    if ch in _BIDI:
        return ContentFlag.BIDI_CONTROL
    if 0xE0000 <= cp <= 0xE007F:
        return ContentFlag.UNICODE_TAGS
    if 0xFE00 <= cp <= 0xFE0F or 0xE0100 <= cp <= 0xE01EF:
        return ContentFlag.VARIATION_SELECTORS
    category = unicodedata.category(ch)
    if category == "Cf":
        return ContentFlag.ZERO_WIDTH  # other invisible format characters
    if category in ("Cc", "Cs") or 0x80 <= cp <= 0x9F:
        return ContentFlag.CONTROL_CHARS
    return None


def strip_invisible(text: str) -> tuple[str, frozenset[ContentFlag], int]:
    flags: set[ContentFlag] = set()
    out: list[str] = []
    removed = 0
    for ch in text.replace("\r\n", "\n").replace("\r", "\n"):
        flag = _char_flag(ch)
        if flag is None:
            out.append(ch)
        else:
            flags.add(flag)
            removed += 1
    return unicodedata.normalize("NFC", "".join(out)), frozenset(flags), removed


# --- HTML ------------------------------------------------------------------------------------------

_SKIP_TAGS: Final = frozenset(
    {
        "script",
        "style",
        "head",
        "title",
        "template",
        "noscript",
        "iframe",
        "object",
        "embed",
        "svg",
        "math",
        "meta",
        "link",
        "base",
        "select",
        "option",
        "textarea",
        "button",
        "canvas",
        "audio",
        "video",
    }
)
_VOID_TAGS: Final = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
)
_BLOCK_TAGS: Final = frozenset(
    {
        "p",
        "div",
        "br",
        "li",
        "tr",
        "td",
        "th",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "table",
        "ul",
        "ol",
        "section",
        "article",
        "blockquote",
        "pre",
        "hr",
        "header",
        "footer",
    }
)
_WHITE: Final = r"(?:white|#fff(?:fff)?\b|rgba?\(\s*255\s*,\s*255\s*,\s*255\s*(?:,\s*[\d.]+\s*)?\)|transparent|hsla?\(\s*\d+\s*,\s*\d+%\s*,\s*100%)"
_HIDING_CSS: Final = re.compile(
    r"display\s*:\s*none"
    r"|visibility\s*:\s*(?:hidden|collapse)"
    r"|font-size\s*:\s*0*\.?0*(?:px|pt|em|rem|%|vw|vh)?\s*(?:;|$|!)"
    r"|font-size\s*:\s*0*\.?[0-4](?:px|pt)\b"
    r"|opacity\s*:\s*0*\.?0*\s*(?:;|$|!)"
    r"|(?<![-\w])color\s*:\s*" + _WHITE + r"|(?:max-)?(?:height|width)\s*:\s*0(?:px|em|rem|%)?\s*(?:;|$|!)"
    r"|(?:left|top|right|bottom|text-indent|margin-left|margin-top)\s*:\s*-\s*\d{3,}"
    r"|clip(?:-path)?\s*:\s*(?:rect\(\s*0|inset\(\s*(?:50|100)%)"
    r"|transform\s*:\s*scale\(\s*0",
    re.IGNORECASE,
)
_FONT_WHITE: Final = re.compile(r"^\s*" + _WHITE + r"\s*$", re.IGNORECASE)


def _hides(tag: str, attrs: dict[str, str | None]) -> bool:
    if "hidden" in attrs:
        return True
    if (attrs.get("aria-hidden") or "").strip().lower() == "true":
        return True
    if tag == "input" and (attrs.get("type") or "").strip().lower() == "hidden":
        return True
    style = attrs.get("style") or ""
    if _HIDING_CSS.search(style):
        return True
    if tag == "font":
        if _FONT_WHITE.match(attrs.get("color") or ""):
            return True
        if (attrs.get("size") or "").strip() in ("0", "-7", "-6"):
            return True
    return False


class _VisibleText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.stack: list[tuple[str, bool]] = []  # (tag, hides its subtree)
        self.hidden_seen = False

    @property
    def _hidden(self) -> bool:
        return any(h for _, h in self.stack)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        amap = {k.lower(): v for k, v in attrs}
        attr_hidden = _hides(tag, amap)
        if attr_hidden:
            self.hidden_seen = True
        if tag in _BLOCK_TAGS and not self._hidden:
            self.parts.append("\n")
        if tag in _VOID_TAGS:
            return
        self.stack.append((tag, attr_hidden or tag in _SKIP_TAGS))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _BLOCK_TAGS and not self._hidden:
            self.parts.append("\n")
        if _hides(tag, {k.lower(): v for k, v in attrs}):
            self.hidden_seen = True

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break
        if tag in _BLOCK_TAGS and not self._hidden:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.stack:
            self.parts.append(data)
            return
        if self._hidden:
            if self.stack[-1][0] == "style" and _HIDING_CSS.search(data):
                self.hidden_seen = True  # a stylesheet that can hide content we cannot resolve
            elif self.stack[-1][0] not in _SKIP_TAGS and data.strip():
                self.hidden_seen = True
            return
        self.parts.append(data)

    def handle_comment(self, data: str) -> None:
        if data.strip():
            self.hidden_seen = True


_BLANK_LINES: Final = re.compile(r"\n[ \t]*\n(?:[ \t]*\n)+")
_SPACES: Final = re.compile(r"[ \t]{2,}")


def html_visible_text(html: str) -> tuple[str, bool]:
    parser = _VisibleText()
    parser.feed(html)
    parser.close()
    text = _SPACES.sub(" ", "".join(parser.parts))
    return _BLANK_LINES.sub("\n\n", text).strip(), parser.hidden_seen


# --- entry point -----------------------------------------------------------------------------------


def sanitize(
    raw: bytes, media_type: str, *, max_raw_bytes: int = MAX_RAW_BYTES, max_chars: int = MAX_VISIBLE_CHARS
) -> Sanitized:
    flags: set[ContentFlag] = set()
    if len(raw) > max_raw_bytes:
        raw = raw[:max_raw_bytes]
        flags.add(ContentFlag.TRUNCATED)
    text = raw.decode("utf-8", "replace")
    if "�" in text and b"\xef\xbf\xbd" not in raw:
        flags.add(ContentFlag.INVALID_UTF8)
    # Strip invisible characters first, so they cannot split tag names or hiding CSS.
    text, char_flags, removed = strip_invisible(text)
    flags |= char_flags
    if media_type == "text/html":
        text, hidden = html_visible_text(text)
        if hidden:
            flags.add(ContentFlag.HIDDEN_HTML)
        # Character references (&#8203;, &#xE0041;) decode to invisible characters after parsing.
        text, char_flags, removed_after = strip_invisible(text)
        flags |= char_flags
        removed += removed_after
    if len(text) > max_chars:
        text = text[:max_chars]
        flags.add(ContentFlag.TRUNCATED)
    return Sanitized(text=text, flags=frozenset(flags), removed_chars=removed, raw_bytes=len(raw))
