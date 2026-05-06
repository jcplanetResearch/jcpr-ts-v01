"""
포지션 SQLite 저장소 (Position SQLite Store)
=============================================

JCPR Trading System - jcpr-ts-v01
Task 25 v0.1

현재 포지션 + 변경 이력.
(Current positions + change history.)

Zone D (Local Only).
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

from .position_state import PositionState

logger = logging.getLogger(__name__)


_TBL_POS = "positions"
_TBL_HIST = "position_history"

_SCHEMA_POS = f"""
CREATE TABLE IF NOT EXISTS {_TBL_POS} (
    symbol             TEXT PRIMARY KEY,
    quantity           INTEGER NOT NULL,
    avg_cost_krw       TEXT NOT NULL,
    realized_pnl_krw   TEXT NOT NULL,
    total_fees_krw     TEXT NOT NULL,
    total_taxes_krw    TEXT NOT NULL,
    last_updated_utc   TEXT,
    fills_processed    INTEGER NOT NULL
)
"""

_SCHEMA_HIST = f"""
CREATE TABLE IF NOT EXISTS {_TBL_HIST} (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol                      TEXT NOT NULL,
    fill_id                     TEXT NOT NULL,
    quantity                    INTEGER NOT NULL,
    avg_cost_krw                TEXT NOT NULL,
    realized_pnl_delta_krw      TEXT NOT NULL,
    realized_pnl_cumulative_krw TEXT NOT NULL,
    timestamp_utc               TEXT NOT NULL
)
"""

_INDEX_HIST = f"""
CREATE INDEX IF NOT EXISTS idx_{_TBL_HIST}_sym_time
ON {_TBL_HIST} (symbol, timestamp_utc)
"""


class PositionStore:
    """SQLite 기반 포지션 저장소."""

    def __init__(self, db_path: str | Path):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        with sqlite3.connect(self._path) as conn:
            conn.execute(_SCHEMA_POS)
            conn.execute(_SCHEMA_HIST)
            conn.execute(_INDEX_HIST)
            conn.commit()

    @property
    def path(self) -> Path:
        return self._path

    # ---------- 현재 포지션 ----------

    def upsert(self, state: PositionState) -> None:
        ts_iso = (
            state.last_updated_utc.astimezone(timezone.utc).isoformat()
            if state.last_updated_utc else None
        )
        with sqlite3.connect(self._path) as conn:
            conn.execute(f"""
                INSERT OR REPLACE INTO {_TBL_POS} (
                    symbol, quantity, avg_cost_krw, realized_pnl_krw,
                    total_fees_krw, total_taxes_krw,
                    last_updated_utc, fills_processed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                state.symbol,
                state.quantity,
                str(state.avg_cost_krw),
                str(state.realized_pnl_krw),
                str(state.total_fees_krw),
                str(state.total_taxes_krw),
                ts_iso,
                state.fills_processed,
            ))
            conn.commit()

    def get(self, symbol: str) -> Optional[PositionState]:
        with sqlite3.connect(self._path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                f"SELECT * FROM {_TBL_POS} WHERE symbol = ?", (symbol,),
            )
            row = cur.fetchone()
            return self._row_to_state(row) if row else None

    def get_all(self, *, only_active: bool = True) -> dict[str, PositionState]:
        with sqlite3.connect(self._path) as conn:
            conn.row_factory = sqlite3.Row
            sql = f"SELECT * FROM {_TBL_POS}"
            if only_active:
                sql += " WHERE quantity > 0"
            sql += " ORDER BY symbol"
            cur = conn.execute(sql)
            return {r["symbol"]: self._row_to_state(r) for r in cur.fetchall()}

    def delete(self, symbol: str) -> None:
        """주의: 정상 운영 중에는 호출 안 함 — 재구축/리셋용."""
        with sqlite3.connect(self._path) as conn:
            conn.execute(f"DELETE FROM {_TBL_POS} WHERE symbol = ?", (symbol,))
            conn.commit()

    def truncate(self) -> None:
        """전체 삭제 — Task 28 reconciliation 시 재구축용."""
        with sqlite3.connect(self._path) as conn:
            conn.execute(f"DELETE FROM {_TBL_POS}")
            conn.execute(f"DELETE FROM {_TBL_HIST}")
            conn.commit()

    @staticmethod
    def _row_to_state(row: sqlite3.Row) -> PositionState:
        ts_str = row["last_updated_utc"]
        ts = datetime.fromisoformat(ts_str) if ts_str else None
        return PositionState(
            symbol=row["symbol"],
            quantity=int(row["quantity"]),
            avg_cost_krw=Decimal(row["avg_cost_krw"]),
            realized_pnl_krw=Decimal(row["realized_pnl_krw"]),
            total_fees_krw=Decimal(row["total_fees_krw"]),
            total_taxes_krw=Decimal(row["total_taxes_krw"]),
            last_updated_utc=ts,
            fills_processed=int(row["fills_processed"]),
        )

    # ---------- 변경 이력 ----------

    def append_history(
        self,
        symbol: str,
        fill_id: str,
        new_state: PositionState,
        realized_delta_krw: Decimal,
        timestamp_utc: datetime,
    ) -> None:
        if timestamp_utc.tzinfo is None:
            raise ValueError("timestamp_utc tz-aware 필수")
        with sqlite3.connect(self._path) as conn:
            conn.execute(f"""
                INSERT INTO {_TBL_HIST} (
                    symbol, fill_id, quantity, avg_cost_krw,
                    realized_pnl_delta_krw, realized_pnl_cumulative_krw,
                    timestamp_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                symbol, fill_id, new_state.quantity,
                str(new_state.avg_cost_krw),
                str(realized_delta_krw),
                str(new_state.realized_pnl_krw),
                timestamp_utc.astimezone(timezone.utc).isoformat(),
            ))
            conn.commit()

    def history(
        self,
        symbol: str,
        *,
        start_utc: Optional[datetime] = None,
        end_utc: Optional[datetime] = None,
    ) -> list[dict]:
        sql = f"SELECT * FROM {_TBL_HIST} WHERE symbol = ?"
        params: list = [symbol]
        if start_utc:
            if start_utc.tzinfo is None:
                raise ValueError("start_utc tz-aware 필수")
            sql += " AND timestamp_utc >= ?"
            params.append(start_utc.astimezone(timezone.utc).isoformat())
        if end_utc:
            if end_utc.tzinfo is None:
                raise ValueError("end_utc tz-aware 필수")
            sql += " AND timestamp_utc <= ?"
            params.append(end_utc.astimezone(timezone.utc).isoformat())
        sql += " ORDER BY timestamp_utc ASC, id ASC"

        with sqlite3.connect(self._path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(sql, params)
            return [
                {
                    "id": r["id"],
                    "symbol": r["symbol"],
                    "fill_id": r["fill_id"],
                    "quantity": r["quantity"],
                    "avg_cost_krw": r["avg_cost_krw"],
                    "realized_pnl_delta_krw": r["realized_pnl_delta_krw"],
                    "realized_pnl_cumulative_krw": r["realized_pnl_cumulative_krw"],
                    "timestamp_utc": r["timestamp_utc"],
                }
                for r in cur.fetchall()
            ]
