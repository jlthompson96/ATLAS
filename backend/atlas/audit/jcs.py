"""RFC 8785 (JSON Canonicalization Scheme) for the subset audit records use.

Audit records contain no floating-point numbers (audit-record.v1), so number formatting reduces to
integers. Everything else follows the RFC:

* object members sorted by the UTF-16 code units of their names (not by code point);
* strings escaped the way ECMAScript ``JSON.stringify`` does (short escapes for ``\\b \\f \\n \\r \\t``,
  lower-case ``\\u00xx`` for other control characters, ``\\"`` and ``\\\\``; everything else, including
  non-ASCII, literally, as UTF-8);
* no insignificant whitespace.

Floats, non-string keys and integers outside the IEEE-754 safe range raise ``ValueError``/``TypeError``
rather than being serialized: a canonical form that is ambiguous would break the hash chain.
"""

from __future__ import annotations

import re
from typing import Any, Final

MAX_SAFE_INT: Final = 2**53 - 1

_SHORT_ESCAPES: Final = {0x08: "\\b", 0x09: "\\t", 0x0A: "\\n", 0x0C: "\\f", 0x0D: "\\r", 0x22: '\\"', 0x5C: "\\\\"}
# Everything JSON.stringify escapes: control characters, '"', '\\' and lone surrogates (well-formed JSON.stringify).
# Python str never holds a surrogate pair, so any surrogate here is a lone one.
_NEEDS_ESCAPE: Final = re.compile('[\x00-\x1f"\\\\\ud800-\udfff]')


def _escape(match: re.Match[str]) -> str:
    cp = ord(match.group())
    return _SHORT_ESCAPES.get(cp) or f"\\u{cp:04x}"


def _string(value: str) -> str:
    # One C-level scan instead of a Python loop per character (known issue N6: verifying a year of records at
    # startup re-hashes every one). Records are mostly IDs and hex hashes, so the substitution rarely fires.
    return '"' + _NEEDS_ESCAPE.sub(_escape, value) + '"'


def _utf16_key(name: str) -> bytes:
    return name.encode("utf-16-be", "surrogatepass")


def _encode(value: Any, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, int):
        if abs(value) > MAX_SAFE_INT:
            raise ValueError("integer outside the IEEE-754 safe range")
        out.append(str(value))
    elif isinstance(value, float):
        raise TypeError("floating-point numbers are not allowed in audit records")
    elif isinstance(value, str):
        out.append(_string(value))
    elif isinstance(value, list | tuple):
        out.append("[")
        for i, item in enumerate(value):
            if i:
                out.append(",")
            _encode(item, out)
        out.append("]")
    elif isinstance(value, dict):
        out.append("{")
        if not all(isinstance(key, str) for key in value):
            raise TypeError("object keys must be strings")
        for i, key in enumerate(sorted(value, key=_utf16_key)):
            if i:
                out.append(",")
            out.append(_string(key))
            out.append(":")
            _encode(value[key], out)
        out.append("}")
    else:
        raise TypeError(f"unsupported type {type(value).__name__}")


def canonicalize(value: Any) -> bytes:
    """Return the JCS serialization of ``value`` as UTF-8 bytes."""
    out: list[str] = []
    _encode(value, out)
    return "".join(out).encode("utf-8", "strict")
