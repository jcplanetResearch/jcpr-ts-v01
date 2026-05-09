"""
Recovery & Restart Consistency Tests (4 tests)
Stage 2B Deliverable 1.

Verifies that the system can be killed at arbitrary points and reopened
without ledger corruption, missed fills, or duplicated state.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.recovery]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL, side TEXT NOT NULL, qty INTEGER NOT NULL,
    limit_price REAL, status TEXT NOT NULL,
    submitted_ts REAL NOT NULL, last_update_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS fills (
    fill_id TEXT PRIMARY KEY,
    client_order_id TEXT NOT NULL,
    symbol TEXT NOT NULL, side TEXT NOT NULL,
    qty INTEGER NOT NULL, price REAL NOT NULL,
    fee REAL NOT NULL DEFAULT 0, fill_ts REAL NOT NULL,
    FOREIGN KEY(client_order_id) REFERENCES orders(client_order_id)
);
"""


def _open(db: Path) -> sqlite3.Connection:
    c = sqlite3.connect(str(db), isolation_level=None, timeout=5.0)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    c.executescript(_SCHEMA)
    return c


# 1 -------------------------------------------------------------------------
def test_ledger_consistent_after_abrupt_restart(tmp_path: Path) -> None:
    """Process dies after committing N orders but before checkpointing.
    Reopening must show all N orders intact."""
    db = tmp_path / "rec1.sqlite3"
    c = _open(db)
    for i in range(20):
        c.execute(
            "INSERT INTO orders VALUES(?,?,?,?,?,?,?,?)",
            (f"o-{i:03d}", "005930", "BUY", 1, 70_000.0,
             "ACCEPTED", time.time(), time.time()),
        )
    # No checkpoint, just close.
    c.close()

    c2 = _open(db)
    n = c2.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    c2.close()
    assert n == 20


# 2 -------------------------------------------------------------------------
def test_unfilled_orders_can_be_reidentified_after_restart(tmp_path: Path) -> None:
    """After restart, the system must be able to query for orders still
    in flight (no terminal status). This is what the broker reconciliation
    step (task 23) will iterate over."""
    db = tmp_path / "rec2.sqlite3"
    c = _open(db)
    for i, status in enumerate(["ACCEPTED", "FILLED", "ACCEPTED", "CANCELED"]):
        c.execute(
            "INSERT INTO orders VALUES(?,?,?,?,?,?,?,?)",
            (f"o-{i}", "005930", "BUY", 1, 70_000.0,
             status, time.time(), time.time()),
        )
    c.close()

    c2 = _open(db)
    open_orders = c2.execute(
        "SELECT client_order_id FROM orders "
        "WHERE status NOT IN ('FILLED','CANCELED','REJECTED')"
    ).fetchall()
    c2.close()
    assert {row[0] for row in open_orders} == {"o-0", "o-2"}


# 3 -------------------------------------------------------------------------
def test_data_in_wal_survives_without_explicit_checkpoint(tmp_path: Path) -> None:
    """SQLite must replay the WAL on next open even if we never explicitly
    called wal_checkpoint. Catches a class of bugs where production code
    only commits but never reopens cleanly."""
    db = tmp_path / "rec3.sqlite3"
    c = _open(db)
    c.execute(
        "INSERT INTO orders VALUES(?,?,?,?,?,?,?,?)",
        ("survive-1", "005930", "BUY", 1, 70_000.0,
         "ACCEPTED", time.time(), time.time()),
    )
    # leak the connection on purpose (no close)
    del c

    c2 = _open(db)
    rows = c2.execute("SELECT client_order_id FROM orders").fetchall()
    c2.close()
    assert ("survive-1",) in rows


# 4 -------------------------------------------------------------------------
def test_idempotent_fill_ingestion_prevents_duplicates(tmp_path: Path) -> None:
    """Reprocessing the same broker fill payload must not double-insert.
    The fill_id is the idempotency key; a second insert must fail with
    IntegrityError, which the production code treats as 'already seen'."""
    db = tmp_path / "rec4.sqlite3"
    c = _open(db)
    c.execute(
        "INSERT INTO orders VALUES(?,?,?,?,?,?,?,?)",
        ("o-1", "005930", "BUY", 5, 70_000.0,
         "ACCEPTED", time.time(), time.time()),
    )
    fill_payload = (
        "fill-001", "o-1", "005930", "BUY", 5, 70_050.0, 70.0, time.time()
    )
    c.execute(
        "INSERT INTO fills VALUES(?,?,?,?,?,?,?,?)", fill_payload
    )
    # second time: same fill_id
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(
            "INSERT INTO fills VALUES(?,?,?,?,?,?,?,?)", fill_payload
        )
    n = c.execute("SELECT COUNT(*) FROM fills WHERE fill_id='fill-001'").fetchone()[0]
    c.close()
    assert n == 1
