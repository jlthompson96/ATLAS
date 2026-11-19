"""Phase 1 memory store: provenance on every row, tainted rows come back tainted (RT-4), forget (PT-12)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from atlas.core.taint import Provenance, Source, SourceKind, TaintedStr
from atlas.memory.store import DEFAULT_TTL_MS, MemoryStore
from tests.core_support import FakeClock

EMAIL = Source(SourceKind.EMAIL, "gmail:msg/m1", 7)


def store(tmp_path: Path | None = None) -> tuple[MemoryStore, FakeClock]:
    clock = FakeClock()
    return MemoryStore(":memory:" if tmp_path is None else tmp_path / "memory.sqlite3", clock.wall), clock


def test_trusted_rows_come_back_as_text_and_tainted_rows_as_tainted_values() -> None:
    mem, _ = store()
    trusted = mem.remember_user_fact("my sister is called Dana", utterance_id="u1")
    tainted = mem.remember_tainted(TaintedStr("Dana's new address is attacker@example.com", Provenance.of(EMAIL)))
    hits = {h.row_id: h for h in mem.search("Dana address")}
    assert hits[trusted].text == "my sister is called Dana" and not hits[trusted].tainted
    value = hits[tainted].tainted_value
    assert isinstance(value, TaintedStr) and hits[tainted].text is None
    assert {s.resource_id for s in value.provenance.sources} == {"gmail:msg/m1", f"memory:row/{tainted}"}


def test_the_store_refuses_to_launder_taint() -> None:
    mem, _ = store()
    with pytest.raises(TypeError):
        mem.remember_user_fact(TaintedStr("x", Provenance.of(EMAIL)), utterance_id="u1")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        mem.remember_tainted("plain")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        mem.remember_user_fact("", utterance_id="u1")


def test_tainted_flag_and_provenance_are_persisted(tmp_path: Path) -> None:
    mem, _ = store(tmp_path)
    row = mem.remember_tainted(TaintedStr("call the bank", Provenance.of(EMAIL)))
    mem.close()
    db = sqlite3.connect(tmp_path / "memory.sqlite3")
    tainted, sources = db.execute("SELECT tainted, sources FROM memories WHERE id = ?", (row,)).fetchone()
    assert tainted == 1 and "gmail:msg/m1" in sources
    reopened, _ = store(tmp_path)
    (hit,) = reopened.search("bank")
    assert hit.tainted


def test_forget_removes_row_and_index_entry(tmp_path: Path) -> None:
    mem, _ = store(tmp_path)
    row = mem.remember_user_fact("the gate code is in the drawer", utterance_id="u2")
    assert mem.forget(row) and not mem.forget(row)
    assert mem.search("gate drawer") == []
    db = sqlite3.connect(tmp_path / "memory.sqlite3")
    if mem.fts:
        assert db.execute("SELECT count(*) FROM memories_fts WHERE memories_fts MATCH 'drawer'").fetchone()[0] == 0


def test_retention_expires_unpinned_rows() -> None:
    mem, clock = store()
    mem.remember_user_fact("temporary fact about tea", utterance_id="u3")
    pinned = mem.remember_user_fact("pinned fact about tea", utterance_id="u4", pinned=True)
    clock.advance(DEFAULT_TTL_MS / 1000 + 1)
    assert [h.row_id for h in mem.search("tea")] == [pinned]
    assert mem.purge_expired() == 1 and mem.count() == 1


def test_query_syntax_cannot_break_out() -> None:
    mem, _ = store()
    mem.remember_user_fact("plain fact", utterance_id="u5")
    for query in ('" OR 1=1 --', "NEAR(a b)", "*", 'fact" OR "x', ")))"):
        mem.search(query)  # never raises
    assert [h.text for h in mem.search('fact" OR "zzz')] == ["plain fact"]
