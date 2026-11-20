"""On-disk layout shared by the writer and the verifier.

``<data_dir>/segments/seg-<first_seq:010d>.jsonl``: one JCS-serialized record per line, ``\\n`` terminated.
``<data_dir>/checkpoints.jsonl``: append-only copy of every checkpoint record (truncation evidence).
``<data_dir>/checkpoint.pub``: unpadded base64url Ed25519 public key (informational; verifiers should
be given the key from a trusted off-box copy).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final, NoReturn

SEGMENT_RE: Final = re.compile(r"seg-([0-9]{10})\.jsonl")
SEGMENTS_DIR: Final = "segments"
CHECKPOINT_SIDECAR: Final = "checkpoints.jsonl"
PUBLIC_KEY_FILE: Final = "checkpoint.pub"
MAX_LINE_BYTES: Final = 256 * 1024


def segment_id(first_seq: int) -> str:
    return f"seg-{first_seq:010d}"


def segment_filename(first_seq: int) -> str:
    return f"{segment_id(first_seq)}.jsonl"


def list_segments(data_dir: Path) -> list[tuple[int, Path]]:
    """Segment files in ``data_dir`` ordered by first seq. Unrecognised names are ignored."""
    directory = data_dir / SEGMENTS_DIR
    found: list[tuple[int, Path]] = []
    if directory.is_dir():
        for path in directory.iterdir():
            match = SEGMENT_RE.fullmatch(path.name)
            if match and path.is_file() and not path.is_symlink():
                found.append((int(match.group(1)), path))
    return sorted(found)


def read_lines(path: Path) -> tuple[list[bytes], bool]:
    """Complete (newline-terminated) lines of ``path`` and whether an unterminated tail was present.

    An unterminated tail is a torn write: it was never acknowledged (the writer always writes the
    newline in the same ``write``) and is ignored.
    """
    data = path.read_bytes()
    if not data:
        return [], False
    torn = not data.endswith(b"\n")
    parts = data.split(b"\n")
    parts.pop()  # empty string after the final newline, or the torn tail
    return parts, torn


def iter_lines(path: Path) -> Iterator[bytes]:
    """Stream the lines of ``path`` without reading the whole file (known issue N6: the verifier's memory).

    Each complete line is yielded with its ``\\n``. An unterminated tail (a torn write, see ``read_lines``) is
    yielded last without one, so the caller can tell the two apart. A line longer than ``MAX_LINE_BYTES``
    raises ``ValueError`` instead of being buffered: the writer never produces one (requests are capped far
    below it), so this is damage or tampering, and holding it in memory is what this function exists to avoid.
    """
    limit = MAX_LINE_BYTES + 1  # room for the newline
    with path.open("rb") as handle:
        while line := handle.readline(limit):
            if len(line) == limit and not line.endswith(b"\n"):
                raise ValueError("line too long")
            yield line


def _reject_constant(name: str) -> NoReturn:
    raise ValueError(f"invalid JSON constant {name}")


def _reject_float(text: str) -> NoReturn:
    raise ValueError("floating-point number in audit record")


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key {key!r}")
        out[key] = value
    return out


def parse_record_line(line: bytes) -> dict[str, Any]:
    """Strict parse: UTF-8, no duplicate keys, no floats, no NaN/Infinity, top level object."""
    if len(line) > MAX_LINE_BYTES:
        raise ValueError("line too long")
    try:
        value = json.loads(
            line.decode("utf-8"),
            parse_float=_reject_float,
            parse_constant=_reject_constant,
            object_pairs_hook=_no_duplicates,
        )
    except RecursionError:
        raise ValueError("nesting too deep") from None
    if not isinstance(value, dict):
        raise ValueError("record is not an object")
    return value
