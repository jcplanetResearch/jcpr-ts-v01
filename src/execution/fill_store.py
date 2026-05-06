"""
체결 SQLite 저장소 (Fill SQLite Store)
========================================

JCPR Trading System - jcpr-ts-v01
Task 24 v0.1

체결 정보 영속화 + 조회.
(Fill persistence + queries.)

Zone D (Local Only) — `.gitignore` 처리됨.

원칙:
- 멱등 upsert (fill_id PK)
- Decimal은 TEXT 저장 (정밀도 보존)
- 모든 datetime UTC tz-aware
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Optional

from .fills import Fill, FillSide

logger = logging.getLogger(__name__)


_TABLE = "fills"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    fill_id          TEXT PRIMARY KEY,
    broker_order_no  TEXT NOT NULL,
    client_order_id  TEXT NOT NULL,
    symbol           TEXT NOT NULL,
    side             TEXT NOT NULL,
    quantity         INTEGER NOT NULL,
    price            TEXT NOT NULL,
    fee_krw          TEXT NOT NULL,
    tax_krw          TEXT NOT NULL,
    filled_at_utc    TEXT NOT NULL,
    received_at_utc  TEXT NOT NULL,
    source           TEXT NOT NULL,
    is_partial       INTEGER NOT NULL,
    raw              TEXT
)
"""

_INDEX_SYM_TIME = f"""
CREATE INDEX IF NOT EXISTS idx_{_TABLE}_sym_time
ON {_TABLE} (symbol, filled_at_utc)
"""

_INDEX_ORDER = f"""
CREATE INDEX IF NOT EXISTS idx_{_TABLE}_order
ON {_TABLE} (broker_order_no)
"""

_INDEX_CLIENT = f"""
CREATE INDEX IF NOT EXISTS idx_{_TABLE}_client
ON {_TABLE} (client_order_id)
"""


class FillStore:
    """SQLite 기반 체결 저장소."""

    def __init__(self, db_path: str | Path):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        with sqlite3.connect(self._path) as conn:
            conn.execute(_SCHEMA)
            conn.execute(_INDEX_SYM_TIME)
            conn.execute(_INDEX_ORDER)
            conn.execute(_INDEX_CLIENT)
            conn.commit()

    @property
    def path(self) -> Path:
        return self._path

    # ---------- 저장 ----------

    def upsert(self, fill: Fill) -> None:
        self.upsert_many([fill])

    def upsert_many(self, fills: Iterable[Fill]) -> int:
        """멱등 upsert. Returns: 처리된 행 수."""
        rows = []
        for f in fills:
            rows.append((
                f.fill_id,
                f.broker_order_no,
                f.client_order_id,
                f.symbol,
                f.side.value,
                f.quantity,
                str(f.price),
                str(f.fee_krw),
                str(f.tax_krw),
                f.filled_at_utc.astimezone(timezone.utc).isoformat(),
                f.received_at_utc.astimezone(timezone.utc).isoformat(),
                f.source,
                1 if f.is_partial else 0,
                json.dumps(f.raw, ensure_ascii=False) if f.raw else None,
            ))
        if not rows:
            return 0

        sql = f"""
            INSERT OR REPLACE INTO {_TABLE} (
                fill_id, broker_order_no, client_order_id, symbol, side,
                quantity, price, fee_krw, tax_krw,
                filled_at_utc, received_at_utc, source, is_partial, raw
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        with sqlite3.connect(self._path) as conn:
            cur = conn.cursor()
            cur.executemany(sql, rows)
            conn.commit()
        logger.info("Fill upsert: %d rows", len(rows))
        return len(rows)

    # ---------- 조회 ----------

    def fetch_by_symbol(
        self,
        symbol: str,
        *,
        start_utc: Optional[datetime] = None,
        end_utc: Optional[datetime] = None,
    ) -> list[Fill]:
        """종목별 체결 조회 (시간 오름차순)."""
        query_parts = [f"SELECT * FROM {_TABLE} WHERE symbol = ?"]
        params: list = [symbol]
        if start_utc is not None:
            if start_utc.tzinfo is None:
                raise ValueError("start_utc tz-aware 필수")
            query_parts.append("AND filled_at_utc >= ?")
            params.append(start_utc.astimezone(timezone.utc).isoformat())
        if end_utc is not None:
            if end_utc.tzinfo is None:
                raise ValueError("end_utc tz-aware 필수")
            query_parts.append("AND filled_at_utc <= ?")
            params.append(end_utc.astimezone(timezone.utc).isoformat())
        query_parts.append("ORDER BY filled_at_utc ASC")

        with sqlite3.connect(self._path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(" ".join(query_parts), params)
            return [self._row_to_fill(r) for r in cur.fetchall()]

    def fetch_by_order(self, broker_order_no: str) -> list[Fill]:
        """특정 주문의 모든 체결 (부분 체결 포함)."""
        with sqlite3.connect(self._path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                f"SELECT * FROM {_TABLE} WHERE broker_order_no = ? "
                f"ORDER BY filled_at_utc ASC",
                (broker_order_no,),
            )
            return [self._row_to_fill(r) for r in cur.fetchall()]

    def fetch_by_client_order_id(self, client_order_id: str) -> list[Fill]:
        """client_order_id로 조회 (멱등 키 추적)."""
        with sqlite3.connect(self._path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                f"SELECT * FROM {_TABLE} WHERE client_order_id = ? "
                f"ORDER BY filled_at_utc ASC",
                (client_order_id,),
            )
            return [self._row_to_fill(r) for r in cur.fetchall()]

    def fetch_since(
        self,
        since_utc: datetime,
        *,
        end_utc: Optional[datetime] = None,
    ) -> list[Fill]:
        """기간 내 모든 체결 (정합성 검증 / Task 28 reconciliation 용)."""
        if since_utc.tzinfo is None:
            raise ValueError("since_utc tz-aware 필수")
        params: list = [since_utc.astimezone(timezone.utc).isoformat()]
        sql = f"SELECT * FROM {_TABLE} WHERE filled_at_utc >= ?"
        if end_utc is not None:
            if end_utc.tzinfo is None:
                raise ValueError("end_utc tz-aware 필수")
            sql += " AND filled_at_utc <= ?"
            params.append(end_utc.astimezone(timezone.utc).isoformat())
        sql += " ORDER BY filled_at_utc ASC"

        with sqlite3.connect(self._path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(sql, params)
            return [self._row_to_fill(r) for r in cur.fetchall()]

    def count(self) -> int:
        with sqlite3.connect(self._path) as conn:
            cur = conn.execute(f"SELECT COUNT(*) FROM {_TABLE}")
            return cur.fetchone()[0]

    def has_fill_id(self, fill_id: str) -> bool:
        with sqlite3.connect(self._path) as conn:
            cur = conn.execute(
                f"SELECT 1 FROM {_TABLE} WHERE fill_id = ? LIMIT 1",
                (fill_id,),
            )
            return cur.fetchone() is not None

    @staticmethod
    def _row_to_fill(row: sqlite3.Row) -> Fill:
        raw_json = row["raw"]
        raw = json.loads(raw_json) if raw_json else {}
        return Fill(
            fill_id=row["fill_id"],
            broker_order_no=row["broker_order_no"],
            client_order_id=row["client_order_id"],
            symbol=row["symbol"],
            side=FillSide(row["side"]),
            quantity=int(row["quantity"]),
            price=Decimal(row["price"]),
            fee_krw=Decimal(row["fee_krw"]),
            tax_krw=Decimal(row["tax_krw"]),
            filled_at_utc=datetime.fromisoformat(row["filled_at_utc"]),
            received_at_utc=datetime.fromisoformat(row["received_at_utc"]),
            source=row["source"],
            is_partial=bool(row["is_partial"]),
            raw=raw,
        )
