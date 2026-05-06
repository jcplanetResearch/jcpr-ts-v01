"""
호가 SQLite 저장소 (Quote SQLite Store)
========================================

JCPR Trading System - jcpr-ts-v01
Task 13 v0.1

스냅샷 + 호가 깊이 (depth) 저장 + 신선도 조회.
(Snapshots + depth storage + freshness queries.)

Zone D (Local Only).

스키마:
- quote_snapshots: 메인 (PK: symbol, captured_at_utc)
- quote_depth: 호가 깊이 (FK to snapshots)
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Optional

from .quote_schema import DepthLevel, QuoteSnapshot

logger = logging.getLogger(__name__)


_TBL_SNAP = "quote_snapshots"
_TBL_DEPTH = "quote_depth"

_SCHEMA_SNAP = f"""
CREATE TABLE IF NOT EXISTS {_TBL_SNAP} (
    symbol            TEXT    NOT NULL,
    captured_at_utc   TEXT    NOT NULL,
    received_at_utc   TEXT    NOT NULL,
    best_bid          TEXT    NOT NULL,
    best_ask          TEXT    NOT NULL,
    best_bid_size     INTEGER NOT NULL,
    best_ask_size     INTEGER NOT NULL,
    last_trade_price  TEXT,
    source            TEXT    NOT NULL,
    is_live_source    INTEGER NOT NULL,
    PRIMARY KEY (symbol, captured_at_utc)
)
"""

_SCHEMA_DEPTH = f"""
CREATE TABLE IF NOT EXISTS {_TBL_DEPTH} (
    symbol           TEXT    NOT NULL,
    captured_at_utc  TEXT    NOT NULL,
    level            INTEGER NOT NULL,
    price            TEXT    NOT NULL,
    bid_size         INTEGER NOT NULL,
    ask_size         INTEGER NOT NULL,
    PRIMARY KEY (symbol, captured_at_utc, level),
    FOREIGN KEY (symbol, captured_at_utc)
        REFERENCES {_TBL_SNAP}(symbol, captured_at_utc)
        ON DELETE CASCADE
)
"""

_INDEX_SNAP_TIME = f"""
CREATE INDEX IF NOT EXISTS idx_{_TBL_SNAP}_sym_time
ON {_TBL_SNAP} (symbol, captured_at_utc DESC)
"""


class QuoteStore:
    """SQLite 호가 저장소."""

    def __init__(self, db_path: str | Path):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        with sqlite3.connect(self._path) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(_SCHEMA_SNAP)
            conn.execute(_SCHEMA_DEPTH)
            conn.execute(_INDEX_SNAP_TIME)
            conn.commit()

    @property
    def path(self) -> Path:
        return self._path

    # ---------- 저장 (Persistence) ----------

    def upsert(self, snap: QuoteSnapshot) -> None:
        """단일 스냅샷 + 호가 깊이 저장 (멱등)."""
        captured_iso = snap.captured_at_utc.astimezone(timezone.utc).isoformat()
        received_iso = snap.received_at_utc.astimezone(timezone.utc).isoformat()

        with sqlite3.connect(self._path) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            cur = conn.cursor()
            # 메인 스냅샷
            cur.execute(f"""
                INSERT OR REPLACE INTO {_TBL_SNAP} VALUES
                (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                snap.symbol, captured_iso, received_iso,
                str(snap.best_bid), str(snap.best_ask),
                snap.best_bid_size, snap.best_ask_size,
                str(snap.last_trade_price) if snap.last_trade_price else None,
                snap.source, 1 if snap.is_live_source else 0,
            ))
            # 기존 depth 제거 (멱등 갱신)
            cur.execute(
                f"DELETE FROM {_TBL_DEPTH} WHERE symbol = ? AND captured_at_utc = ?",
                (snap.symbol, captured_iso),
            )
            # 호가 깊이
            for d in snap.depth_levels:
                cur.execute(f"""
                    INSERT INTO {_TBL_DEPTH} VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    snap.symbol, captured_iso, d.level,
                    str(d.price), d.bid_size, d.ask_size,
                ))
            conn.commit()

    def upsert_many(self, snaps: Iterable[QuoteSnapshot]) -> int:
        n = 0
        for s in snaps:
            self.upsert(s)
            n += 1
        return n

    # ---------- 조회 (Query) ----------

    def latest(self, symbol: str) -> Optional[QuoteSnapshot]:
        """가장 최근 스냅샷."""
        with sqlite3.connect(self._path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(f"""
                SELECT * FROM {_TBL_SNAP}
                WHERE symbol = ?
                ORDER BY captured_at_utc DESC LIMIT 1
            """, (symbol,))
            row = cur.fetchone()
            if row is None:
                return None
            depth = self._fetch_depth(conn, symbol, row["captured_at_utc"])
            return self._row_to_snap(row, depth)

    def latest_fresh(
        self, symbol: str, now_utc: datetime, max_age_sec: int,
    ) -> Optional[QuoteSnapshot]:
        """
        최근 스냅샷 중 신선한 것만 반환.
        (Latest snapshot, only if not stale.)

        fail-closed: stale이면 None.
        """
        snap = self.latest(symbol)
        if snap is None:
            return None
        if snap.is_stale(now_utc, max_age_sec):
            logger.info(
                "stale 호가 거부 (stale quote rejected): symbol=%s age=%.1fs > max=%ds",
                symbol, snap.age_seconds(now_utc), max_age_sec,
            )
            return None
        return snap

    def fetch_range(
        self, symbol: str, start_utc: datetime, end_utc: datetime,
    ) -> list[QuoteSnapshot]:
        """기간 내 모든 스냅샷."""
        if start_utc.tzinfo is None or end_utc.tzinfo is None:
            raise ValueError("start_utc, end_utc tz-aware 필수")
        with sqlite3.connect(self._path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(f"""
                SELECT * FROM {_TBL_SNAP}
                WHERE symbol = ?
                  AND captured_at_utc >= ?
                  AND captured_at_utc <= ?
                ORDER BY captured_at_utc ASC
            """, (
                symbol,
                start_utc.astimezone(timezone.utc).isoformat(),
                end_utc.astimezone(timezone.utc).isoformat(),
            ))
            rows = cur.fetchall()
            result = []
            for row in rows:
                depth = self._fetch_depth(conn, symbol, row["captured_at_utc"])
                result.append(self._row_to_snap(row, depth))
            return result

    @staticmethod
    def _fetch_depth(
        conn: sqlite3.Connection, symbol: str, captured_iso: str,
    ) -> tuple[DepthLevel, ...]:
        cur = conn.execute(f"""
            SELECT * FROM {_TBL_DEPTH}
            WHERE symbol = ? AND captured_at_utc = ?
            ORDER BY level ASC
        """, (symbol, captured_iso))
        rows = cur.fetchall()
        return tuple(
            DepthLevel(
                level=int(r["level"]),
                price=Decimal(r["price"]),
                bid_size=int(r["bid_size"]),
                ask_size=int(r["ask_size"]),
            )
            for r in rows
        )

    @staticmethod
    def _row_to_snap(row: sqlite3.Row, depth: tuple[DepthLevel, ...]) -> QuoteSnapshot:
        return QuoteSnapshot(
            symbol=row["symbol"],
            captured_at_utc=datetime.fromisoformat(row["captured_at_utc"]),
            received_at_utc=datetime.fromisoformat(row["received_at_utc"]),
            best_bid=Decimal(row["best_bid"]),
            best_ask=Decimal(row["best_ask"]),
            best_bid_size=int(row["best_bid_size"]),
            best_ask_size=int(row["best_ask_size"]),
            depth_levels=depth,
            last_trade_price=Decimal(row["last_trade_price"]) if row["last_trade_price"] else None,
            source=row["source"],
            is_live_source=bool(row["is_live_source"]),
        )

    # ---------- 정리 (Cleanup) ----------

    def purge_older_than(self, before_utc: datetime) -> int:
        """오래된 스냅샷 삭제 (depth는 ON DELETE CASCADE)."""
        if before_utc.tzinfo is None:
            raise ValueError("before_utc tz-aware 필수")
        with sqlite3.connect(self._path) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            cur = conn.execute(f"""
                DELETE FROM {_TBL_SNAP} WHERE captured_at_utc < ?
            """, (before_utc.astimezone(timezone.utc).isoformat(),))
            conn.commit()
            return cur.rowcount
