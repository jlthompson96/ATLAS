"""Phase 1 memory store: stdlib ``sqlite3`` + FTS5 keyword search (PLAN.md §2.12, R-MEM).

Every row stores its provenance. Rows written from untrusted content are **tainted** (RT-4):

* ``search`` returns them as ``TaintedStr`` (which the ``search_memory`` tool binds to a ``$MEM_n``
  reference), never as text the planner can read;
* a proposal that uses one carries ``provenance.tainted`` and the ``tainted_args`` warning, so a
  "remember that the user's new address is ..." email can never silently become a recipient.

Trusted rows (things the user said) come back as plain text.

Retention defaults follow §2.12 (30 days; pinned rows are kept until deleted). ``forget`` deletes the
row and its FTS entry (PT-12). The database lives in atlas-core's state directory on the Pi.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from atlas.core.taint import Provenance, Source, SourceKind, TaintedStr

DEFAULT_TTL_MS: Final = 30 * 24 * 3600 * 1000
MAX_ROW_CHARS: Final = 2000
MAX_QUERY_TERMS: Final = 8
_TERM_RE: Final = re.compile(r"[^\W_]{2,32}", re.UNICODE)

_SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY,
    text TEXT NOT NULL,
    tainted INTEGER NOT NULL CHECK (tainted IN (0, 1)),
    sources TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER,
    pinned INTEGER NOT NULL DEFAULT 0
);
"""


@dataclass(frozen=True, slots=True)
class MemoryHit:
    row_id: int
    text: str | None  # set for trusted rows only
    tainted_value: TaintedStr | None  # set for tainted rows only
    created_at_ms: int

    @property
    def resource_id(self) -> str:
        return f"memory:row/{self.row_id}"

    @property
    def tainted(self) -> bool:
        return self.tainted_value is not None


def _sources_json(provenance: Provenance) -> str:
    return json.dumps(
        [[s.kind.value, s.resource_id, s.observed_at_ms] for s in sorted(provenance.sources)], separators=(",", ":")
    )


def _sources_from(text: str) -> Provenance:
    return Provenance(frozenset(Source(SourceKind(k), r, int(t)) for k, r, t in json.loads(text)))


class MemoryStore:
    def __init__(self, path: Path | str, clock_ms: Callable[[], int]) -> None:
        self._clock = clock_ms
        self._db = sqlite3.connect(str(path))
        self._db.execute("PRAGMA journal_mode=WAL" if str(path) != ":memory:" else "PRAGMA journal_mode=MEMORY")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(_SCHEMA)
        self.fts = self._has_fts5()
        if self.fts:
            self._db.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(text, content='memories', content_rowid='id')"
            )

    def _has_fts5(self) -> bool:
        try:
            self._db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp._fts_probe USING fts5(x)")
            self._db.execute("DROP TABLE temp._fts_probe")
        except sqlite3.OperationalError:
            return False
        return True

    def close(self) -> None:
        self._db.close()

    def _insert(self, text: str, tainted: bool, provenance: Provenance, ttl_ms: int | None, pinned: bool) -> int:
        if not text or len(text) > MAX_ROW_CHARS:
            raise ValueError("memory text must be 1..2000 characters")
        now = self._clock()
        expires = None if pinned or ttl_ms is None else now + ttl_ms
        with self._db:
            cur = self._db.execute(
                "INSERT INTO memories (text, tainted, sources, created_at, expires_at, pinned) VALUES (?, ?, ?, ?, ?, ?)",
                (text, int(tainted), _sources_json(provenance), now, expires, int(pinned)),
            )
            row_id = cur.lastrowid
            assert row_id is not None  # noqa: S101 - sqlite always sets it after INSERT
            if self.fts:
                self._db.execute("INSERT INTO memories_fts (rowid, text) VALUES (?, ?)", (row_id, text))
        return row_id

    def remember_user_fact(
        self, text: str, *, utterance_id: str, pinned: bool = False, ttl_ms: int = DEFAULT_TTL_MS
    ) -> int:
        """A fact the user stated themselves (trusted)."""
        if type(text) is not str:
            raise TypeError("trusted memory takes a plain str; tainted text goes through remember_tainted")
        source = Source(SourceKind.USER, f"utterance:{utterance_id}", self._clock())
        return self._insert(text, False, Provenance.of(source), ttl_ms, pinned)

    def remember_tainted(self, value: TaintedStr, *, ttl_ms: int = DEFAULT_TTL_MS) -> int:
        """Memory derived from untrusted content: stored tainted with its full provenance."""
        if not isinstance(value, TaintedStr):
            raise TypeError("remember_tainted takes a TaintedStr")
        return self._insert(value._reveal(), True, value.provenance, ttl_ms, False)

    def search(self, query: str, limit: int = 5) -> list[MemoryHit]:
        terms = _TERM_RE.findall(query.casefold())[:MAX_QUERY_TERMS]
        if not terms:
            return []
        now = self._clock()
        limit = max(1, min(limit, 20))
        if self.fts:
            match = " OR ".join('"' + term.replace('"', "") + '"' for term in terms)
            rows = self._db.execute(
                "SELECT m.id, m.text, m.tainted, m.sources, m.created_at FROM memories_fts f "
                "JOIN memories m ON m.id = f.rowid WHERE memories_fts MATCH ? "
                "AND (m.expires_at IS NULL OR m.expires_at > ?) ORDER BY rank LIMIT ?",
                (match, now, limit),
            ).fetchall()
        else:
            clause = " OR ".join("lower(text) LIKE ?" for _ in terms)
            rows = self._db.execute(
                f"SELECT id, text, tainted, sources, created_at FROM memories WHERE ({clause}) "  # noqa: S608 - placeholders only
                "AND (expires_at IS NULL OR expires_at > ?) ORDER BY id DESC LIMIT ?",
                (*[f"%{t}%" for t in terms], now, limit),
            ).fetchall()
        hits: list[MemoryHit] = []
        for row_id, text, tainted, sources, created in rows:
            if tainted:
                memory_source = Source(SourceKind.MEMORY, f"memory:row/{row_id}", int(created))
                provenance = _sources_from(sources).union(Provenance.of(memory_source))
                hits.append(MemoryHit(int(row_id), None, TaintedStr(text, provenance), int(created)))
            else:
                hits.append(MemoryHit(int(row_id), text, None, int(created)))
        return hits

    def forget(self, row_id: int) -> bool:
        with self._db:
            row = self._db.execute("SELECT text FROM memories WHERE id = ?", (row_id,)).fetchone()
            if row is None:
                return False
            if self.fts:
                self._db.execute(
                    "INSERT INTO memories_fts (memories_fts, rowid, text) VALUES ('delete', ?, ?)", (row_id, row[0])
                )
            self._db.execute("DELETE FROM memories WHERE id = ?", (row_id,))
        return True

    def purge_expired(self) -> int:
        now = self._clock()
        ids = [r[0] for r in self._db.execute("SELECT id FROM memories WHERE expires_at IS NOT NULL AND expires_at <= ?", (now,))]
        for row_id in ids:
            self.forget(int(row_id))
        return len(ids)

    def count(self) -> int:
        return int(self._db.execute("SELECT count(*) FROM memories").fetchone()[0])
