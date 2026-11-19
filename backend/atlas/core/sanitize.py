"""Hidden-content stripping and size bounds for untrusted items, before the quarantined reader (RT-2, RT-11).

What the reader sees is what a human would see, bounded in size:

* **Size.** Raw input is cut before any parsing (``MAX_RAW_BYTES``; ``MAX_HTML_BYTES`` for HTML), and
  the visible text at ``MAX_VISIBLE_CHARS``. Both set ``TRUNCATED``. Parsing is linear in the input:
  nesting deeper than ``MAX_DEPTH`` is treated as hidden, and an end tag only closes an element within
  the last ``END_TAG_SEARCH`` open ones (review F1).
* **HTML.** Only visible text survives. Dropped: ``script``/``style``/``head``/``template``/
  ``noscript``/``iframe``/``object``/``svg`` and similar; comments; elements with the ``hidden``
  attribute, ``aria-hidden="true"`` or ``type=hidden``; inline styles that hide text
  (``display:none``, ``visibility:hidden``, ``mso-hide:all``, tiny font sizes, opacity below 0.1, white,
  transparent or background-coloured text when both colours are inline, zero-size or 1px boxes with
  hidden overflow, off-screen positioning, clipping, big negative indents, ``scale(0)``);
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
MAX_HTML_BYTES: Final = 256 * 1024  # HTML is parsed, so it gets a tighter cap (review F1)
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
# Inline-style hiding (F10). Declarations are parsed one by one; no cascade, no stylesheet resolution.
_NAMED: Final = {
    "white": (255, 255, 255),
    "black": (0, 0, 0),
    "silver": (192, 192, 192),
    "gray": (128, 128, 128),
    "grey": (128, 128, 128),
    "red": (255, 0, 0),
    "green": (0, 128, 0),
    "blue": (0, 0, 255),
    "yellow": (255, 255, 0),
}
_HEX: Final = re.compile(r"#([0-9a-f]{3,8})\b")
_RGB: Final = re.compile(r"rgba?\(\s*(\d{1,3})\s*[, ]\s*(\d{1,3})\s*[, ]\s*(\d{1,3})\s*(?:[,/]\s*([\d.]+%?)\s*)?\)")
_LENGTH: Final = re.compile(r"^(-?\d*\.?\d+)\s*(px|pt|em|rem|%|vw|vh|ex|ch)?$")
_NEAR_COLOUR: Final = 24  # max per-channel difference that counts as "same colour as the background"
_STYLESHEET_HIDING: Final = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*(?:hidden|collapse)|opacity\s*:\s*0|font-size\s*:\s*0|mso-hide\s*:\s*all",
    re.IGNORECASE,
)
_FONT_WHITE: Final = frozenset({"white", "#fff", "#ffffff", "transparent"})
MAX_DEPTH: Final = 256  # deeper nesting is treated as hidden (and flagged)
END_TAG_SEARCH: Final = 32  # how far back an end tag may close an element


def _colour(value: str) -> tuple[int, int, int, float] | None:
    """(r, g, b, alpha) for the first colour in a CSS value, or None."""
    v = value.strip().lower()
    if v == "transparent":
        return (0, 0, 0, 0.0)
    for word in v.split():
        if word in _NAMED:
            return (*_NAMED[word], 1.0)
    m = _HEX.search(v)
    if m:
        h = m.group(1)
        if len(h) in (3, 4):
            h = "".join(c * 2 for c in h)
        if len(h) not in (6, 8):
            return None
        alpha = int(h[6:8], 16) / 255 if len(h) == 8 else 1.0
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), alpha)
    m = _RGB.search(v)
    if m:
        alpha_text = m.group(4)
        alpha = 1.0
        if alpha_text:
            alpha = float(alpha_text[:-1]) / 100 if alpha_text.endswith("%") else float(alpha_text)
        return (min(int(m.group(1)), 255), min(int(m.group(2)), 255), min(int(m.group(3)), 255), alpha)
    return None


def _length(value: str) -> tuple[float, str] | None:
    m = _LENGTH.match(value.strip().lower())
    if m is None:
        return None
    return float(m.group(1)), m.group(2) or "px"


def _declarations(style: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for decl in style.split(";"):
        name, sep, value = decl.partition(":")
        if sep:
            out[name.strip().lower()] = value.lower().replace("!important", "").strip()
    return out


def _tiny_font(value: str) -> bool:
    length = _length(value)
    if length is None:
        return False
    n, unit = length
    limits = {"px": 1.5, "pt": 1.5, "em": 0.2, "rem": 0.2, "%": 20, "ex": 0.4, "ch": 0.4, "vw": 0.1, "vh": 0.1}
    return n < limits.get(unit, 1.0)


def _style_hides(style: str) -> bool:
    d = _declarations(style)
    if d.get("display") == "none" or d.get("visibility") in ("hidden", "collapse"):
        return True
    if d.get("mso-hide") == "all":
        return True
    if "font-size" in d and _tiny_font(d["font-size"]):
        return True
    opacity = _length(d.get("opacity", "1"))
    if opacity is not None and (opacity[0] / 100 if opacity[1] == "%" else opacity[0]) < 0.1:
        return True
    clipped = (
        d.get("overflow") in ("hidden", "clip") or d.get("overflow-x") == "hidden" or d.get("overflow-y") == "hidden"
    )
    for prop in ("height", "width", "max-height", "max-width"):
        length = _length(d.get(prop, ""))
        if length is not None and (length[0] == 0 or (clipped and length[1] in ("px", "pt") and length[0] <= 1)):
            return True
    for prop in ("left", "top", "right", "bottom", "text-indent", "margin-left", "margin-top"):
        length = _length(d.get(prop, ""))
        if length is not None and length[0] <= -100:
            return True
    if d.get("clip", "").startswith("rect(0") or re.match(r"inset\(\s*(?:50|100)%", d.get("clip-path", "")):
        return True
    if re.match(r"scale\(\s*0(?:\.0+)?\s*[,)]", d.get("transform", "")):
        return True
    colour = _colour(d["color"]) if "color" in d else None
    if colour is not None:
        if colour[3] < 0.1 or colour[:3] == (255, 255, 255):
            return True
        background = _colour(d.get("background-color", d.get("background", "")))
        if (
            background is not None
            and background[3] >= 0.9
            and max(abs(a - b) for a, b in zip(colour[:3], background[:3], strict=True)) <= _NEAR_COLOUR
        ):
            return True
    return False


def _hides(tag: str, attrs: dict[str, str | None]) -> bool:
    if "hidden" in attrs:
        return True
    if (attrs.get("aria-hidden") or "").strip().lower() == "true":
        return True
    if tag == "input" and (attrs.get("type") or "").strip().lower() == "hidden":
        return True
    if _style_hides(attrs.get("style") or ""):
        return True
    if tag == "font":
        if (attrs.get("color") or "").strip().lower() in _FONT_WHITE:
            return True
        if (attrs.get("size") or "").strip() in ("0", "-7", "-6"):
            return True
    return False


class _VisibleText(HTMLParser):
    """Visible-text extractor. Every callback is O(1) amortized (review F1): a hidden-depth counter
    instead of scanning the stack, a depth cap, and a bounded search for the element an end tag closes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.stack: list[tuple[str, bool]] = []  # (tag, hides its subtree)
        self.hidden_depth = 0  # how many entries on the stack hide their subtree
        self.overflow = False  # nesting went past MAX_DEPTH: everything after is treated as hidden
        self.hidden_seen = False

    @property
    def _hidden(self) -> bool:
        return self.overflow or self.hidden_depth > 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        amap = {k.lower(): v for k, v in attrs}
        attr_hidden = _hides(tag, amap)
        if attr_hidden:
            self.hidden_seen = True
        if tag in _BLOCK_TAGS and not self._hidden:
            self.parts.append("\n")
        if tag in _VOID_TAGS or self.overflow:
            return
        if len(self.stack) >= MAX_DEPTH:
            self.overflow = True
            self.hidden_seen = True
            return
        hides = attr_hidden or tag in _SKIP_TAGS
        self.stack.append((tag, hides))
        self.hidden_depth += hides

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _BLOCK_TAGS and not self._hidden:
            self.parts.append("\n")
        if _hides(tag, {k.lower(): v for k, v in attrs}):
            self.hidden_seen = True

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        stop = max(0, len(self.stack) - END_TAG_SEARCH)
        for i in range(len(self.stack) - 1, stop - 1, -1):
            if self.stack[i][0] == tag:
                self.hidden_depth -= sum(1 for _, h in self.stack[i:] if h)  # at most END_TAG_SEARCH entries
                del self.stack[i:]
                break
        if tag in _BLOCK_TAGS and not self._hidden:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._hidden:
            top = self.stack[-1][0] if self.stack else ""
            if top == "style" and _STYLESHEET_HIDING.search(data):
                self.hidden_seen = True  # a stylesheet that can hide content we cannot resolve
            elif top not in _SKIP_TAGS and data.strip():
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
    if media_type == "text/html":
        max_raw_bytes = min(max_raw_bytes, MAX_HTML_BYTES)
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
