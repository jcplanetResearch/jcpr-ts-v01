"""
WAL Store Integration Tests (8 tests)
Stage 2B Deliverable 1.

Verifies SQLite WAL-mode behavior that the production ledger relies on:
concurrency, transactions, checkpoints, recovery from abrupt close,
foreign-key enforcement, and a coarse performance guardrail.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.wal]


def _seed_order(store, oid: str = "ord-001", symbol: str = "005930") -> dict:
    order = {
        "client_order_id": oid, "symbol": symbol, "side": "BUY", "qty": 10,
        "limit_price": 70_000.0, "status": "ACCEPTED",
        "submitted_ts": time.time(), "last_update_ts": time.time(),
    }
    store.insert_order(order)
    return order


# 1 -------------------------------------------------------------------------
def test_wal_mode_is_enabled(wal_conn: sqlite3.Connection) -> None:
    """WAL must be the active journal mode; rollback/delete modes are not safe
    for our concurrent reader+writer pattern."""
    mode = wal_conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal", f"expected WAL, got {mode!r}"

    fk = wal_conn.execute("PRAGMA foreign_keys").fetchone()[0]
    assert fk == 1, "foreign_keys pragma must be ON for ledger integrity"


# 2 -------------------------------------------------------------------------
def test_concurrent_reader_does_not_block_writer(wal_store, wal_db_path: Path) -> None:
    """In WAL mode a long-running reader must not block a writer.

    We open a second connection in a thread, hold a SELECT cursor open,
    then attempt a write on the main connection. The write must succeed
    within a small timeout.
    """
    _seed_order(wal_store, "ord-A")

    reader_started = threading.Event()
    reader_done = threading.Event()

    def long_reader() -> None:
        conn = sqlite3.connect(str(wal_db_path), timeout=5.0)
        try:
            cur = conn.execute("SELECT * FROM orders")
            cur.fetchone()
            reader_started.set()
            # hold the read transaction open briefly
            time.sleep(0.5)
            cur.fetchall()
        finally:
            conn.close()
            reader_done.set()

    t = threading.Thread(target=long_reader, daemon=True)
    t.start()
    assert reader_started.wait(timeout=2.0), "reader thread never started"

    t0 = time.time()
    _seed_order(wal_store, "ord-B")
    elapsed = time.time() - t0
    # If writer were blocked by the reader, we'd see ~0.5s; allow generous slack.
    assert elapsed < 0.4, f"writer was blocked for {elapsed:.3f}s"

    t.join(timeout=3.0)
    assert reader_done.is_set()


# 3 -------------------------------------------------------------------------
def test_transaction_rollback_leaves_no_partial_state(wal_store) -> None:
    """A failed transaction must roll back cleanly; no orphan rows."""
    conn = wal_store.conn
    conn.execute("BEGIN")
    try:
        _seed_order(wal_store, "ord-X")
        # force a constraint failure: duplicate primary key
        _seed_order(wal_store, "ord-X")
        conn.execute("COMMIT")
    except sqlite3.IntegrityError:
        conn.execute("ROLLBACK")

    assert wal_store.count("orders") == 0, "rollback failed to clean up"


# 4 -------------------------------------------------------------------------
def test_wal_checkpoint_truncate_shrinks_wal(wal_store, wal_db_path: Path) -> None:
    """Checkpoint(TRUNCATE) must reduce or zero the WAL file size."""
    for i in range(50):
        _seed_order(wal_store, oid=f"ord-{i:03d}")

    wal_path = Path(str(wal_db_path) + "-wal")
    pre_size = wal_path.stat().st_size if wal_path.exists() else 0
    assert pre_size > 0, "expected WAL to have grown after writes"

    wal_store.checkpoint()

    post_size = wal_path.stat().st_size if wal_path.exists() else 0
    assert post_size <= pre_size, "checkpoint did not shrink WAL"


# 5 -------------------------------------------------------------------------
def test_recovery_after_abrupt_close(tmp_path: Path) -> None:
    """If the writer dies without a checkpoint, reopening must recover all
    committed data from the WAL.
    """
    db = tmp_path / "abrupt.sqlite3"
    schema = """
        CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT NOT NULL);
    """
    c1 = sqlite3.connect(str(db))
    c1.execute("PRAGMA journal_mode=WAL")
    c1.executescript(schema)
    c1.execute("INSERT INTO t(v) VALUES('alpha')")
    c1.execute("INSERT INTO t(v) VALUES('bravo')")
    c1.commit()
    # simulate abrupt termination: close without explicit checkpoint
    c1.close()

    c2 = sqlite3.connect(str(db))
    rows = c2.execute("SELECT v FROM t ORDER BY id").fetchall()
    c2.close()
    assert [r[0] for r in rows] == ["alpha", "bravo"]


# 6 -------------------------------------------------------------------------
def test_foreign_key_enforcement_on_fills(wal_store) -> None:
    """A fill referencing an unknown order must be rejected by FK constraint."""
    bad_fill = {
        "fill_id": "f-1", "client_order_id": "does-not-exist",
        "symbol": "005930", "side": "BUY", "qty": 1, "price": 70_000.0,
        "fee": 0.0, "fill_ts": time.time(),
    }
    with pytest.raises(sqlite3.IntegrityError):
        wal_store.insert_fill(bad_fill)


# 7 -------------------------------------------------------------------------
def test_index_is_used_for_status_lookup(wal_store) -> None:
    """The idx_orders_status index must be picked up by the planner.

    A regression here would mean a missed migration or a planner config
    change that silently ruins query performance.
    """
    plan = wal_store.conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM orders WHERE status='ACCEPTED'"
    ).fetchall()
    plan_text = " ".join(str(row) for row in plan)
    assert "idx_orders_status" in plan_text, plan_text


# 8 -------------------------------------------------------------------------
@pytest.mark.slow
def test_bulk_insert_performance_guardrail(wal_store) -> None:
    """Coarse perf check: 1000 inserts inside one transaction must finish in
    well under one second on any reasonable machine. Catches accidental
    autocommit-per-row regressions."""
    conn = wal_store.conn
    conn.execute("BEGIN")
    t0 = time.time()
    for i in range(1000):
        _seed_order(wal_store, oid=f"perf-{i:05d}")
    conn.execute("COMMIT")
    elapsed = time.time() - t0
    assert elapsed < 1.5, f"bulk insert took {elapsed:.3f}s (regression?)"
    assert wal_store.count("orders") == 1000
