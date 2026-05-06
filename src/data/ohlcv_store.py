"""
OHLCV SQLite 저장소 (OHLCV SQLite Store)
=========================================

JCPR Trading System - jcpr-ts-v01
Task 12 v0.1

로컬 SQLite에 봉 저장 + 매수/매도 강도 / CVD 조회.
(Local SQLite storage + buy-sell intensity / CVD queries.)

Zone D (Local Only) — 절대 GitHub 추적 금지.

스키마 (Schema):
- 복합 PK: (symbol, timeframe, bar_time_utc)
- 멱등 upsert (INSERT OR REPLACE)
- 가격은 TEXT (Decimal) 저장 — 부동소수점 손실 방지

원칙 (Principles):
- 가격 정밀도 보존 (Decimal as TEXT)
- UTC tz-aware 강제
- fail-closed: 검증 실패 봉은 저장 안 함 (OHLCVBar __post_init__에서 거부)
- 갭 검출 (gap detection) 제공
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Optional

from .ohlcv_schema import OHLCVBar, Timeframe, TickDirection, VolumeSplitMethod

logger = logging.getLogger(__name__)


_TABLE = "ohlcv_bars"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    symbol               TEXT    NOT NULL,
    timeframe            TEXT    NOT NULL,
    bar_time_utc         TEXT    NOT NULL,
    open                 TEXT    NOT NULL,
    high                 TEXT    NOT NULL,
    low                  TEXT    NOT NULL,
    close                TEXT    NOT NULL,
    volume               INTEGER NOT NULL,
    value_krw            TEXT,
    tick_direction       TEXT    NOT NULL DEFAULT 'unknown',
    tick_direction_alt   TEXT    NOT NULL DEFAULT 'unknown',
    up_volume            INTEGER,
    down_volume          INTEGER,
    volume_split_method  TEXT    NOT NULL DEFAULT 'unknown',
    source               TEXT    NOT NULL DEFAULT 'unknown',
    ingested_at_utc      TEXT,
    PRIMARY KEY (symbol, timeframe, bar_time_utc)
)
"""

_INDEX_SYM_TIME = f"""
CREATE INDEX IF NOT EXISTS idx_{_TABLE}_sym_tf_time
ON {_TABLE} (symbol, timeframe, bar_time_utc)
"""


# ─────────────────────────────────────────────────
# OHLCVStore
# ─────────────────────────────────────────────────

class OHLCVStore:
    """SQLite 기반 OHLCV 저장소 (Local-only)."""

    def __init__(self, db_path: str | Path):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        with sqlite3.connect(self._path) as conn:
            conn.execute(_SCHEMA)
            conn.execute(_INDEX_SYM_TIME)
            conn.commit()

    @property
    def path(self) -> Path:
        return self._path

    # ---------- 저장 (Persistence) ----------

    def upsert_bars(self, bars: Iterable[OHLCVBar]) -> int:
        """
        봉을 멱등 upsert. 동일 PK는 갱신.
        (Idempotent upsert — same PK overwrites.)

        Returns:
            저장된 행 수 (rows affected).
        """
        rows = []
        for b in bars:
            if b.bar_time_utc.tzinfo is None:
                raise ValueError(f"bar_time_utc tz-aware 필수: {b.bar_time_utc}")
            rows.append((
                b.symbol,
                b.timeframe.value,
                b.bar_time_utc.astimezone(timezone.utc).isoformat(),
                str(b.open), str(b.high), str(b.low), str(b.close),
                b.volume,
                str(b.value_krw) if b.value_krw is not None else None,
                b.tick_direction.value,
                b.tick_direction_alt.value,
                b.up_volume,
                b.down_volume,
                b.volume_split_method.value,
                b.source,
                b.ingested_at_utc.astimezone(timezone.utc).isoformat()
                    if b.ingested_at_utc is not None else None,
            ))

        if not rows:
            return 0

        sql = f"""
            INSERT OR REPLACE INTO {_TABLE} (
                symbol, timeframe, bar_time_utc,
                open, high, low, close, volume, value_krw,
                tick_direction, tick_direction_alt,
                up_volume, down_volume, volume_split_method,
                source, ingested_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        with sqlite3.connect(self._path) as conn:
            cur = conn.cursor()
            cur.executemany(sql, rows)
            conn.commit()
            n = cur.rowcount
        logger.info("OHLCV upsert 완료: %d rows", n if n >= 0 else len(rows))
        return n if n >= 0 else len(rows)

    # ---------- 조회 (Query) ----------

    def fetch(
        self,
        symbol: str,
        timeframe: Timeframe,
        start_utc: datetime,
        end_utc: datetime,
    ) -> list[OHLCVBar]:
        """기간 내 봉 조회 (시간 오름차순)."""
        if start_utc.tzinfo is None or end_utc.tzinfo is None:
            raise ValueError("start_utc, end_utc는 tz-aware 필수")

        sql = f"""
            SELECT * FROM {_TABLE}
            WHERE symbol = ? AND timeframe = ?
              AND bar_time_utc >= ? AND bar_time_utc <= ?
            ORDER BY bar_time_utc ASC
        """
        with sqlite3.connect(self._path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(sql, (
                symbol, timeframe.value,
                start_utc.astimezone(timezone.utc).isoformat(),
                end_utc.astimezone(timezone.utc).isoformat(),
            ))
            rows = cur.fetchall()

        return [self._row_to_bar(r) for r in rows]

    def latest_bar(self, symbol: str, timeframe: Timeframe) -> Optional[OHLCVBar]:
        """최신 봉 1개."""
        sql = f"""
            SELECT * FROM {_TABLE}
            WHERE symbol = ? AND timeframe = ?
            ORDER BY bar_time_utc DESC LIMIT 1
        """
        with sqlite3.connect(self._path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(sql, (symbol, timeframe.value))
            row = cur.fetchone()
        return self._row_to_bar(row) if row else None

    @staticmethod
    def _row_to_bar(row: sqlite3.Row) -> OHLCVBar:
        return OHLCVBar(
            symbol=row["symbol"],
            timeframe=Timeframe(row["timeframe"]),
            bar_time_utc=datetime.fromisoformat(row["bar_time_utc"]),
            open=Decimal(row["open"]),
            high=Decimal(row["high"]),
            low=Decimal(row["low"]),
            close=Decimal(row["close"]),
            volume=int(row["volume"]),
            value_krw=Decimal(row["value_krw"]) if row["value_krw"] else None,
            tick_direction=TickDirection(row["tick_direction"]),
            tick_direction_alt=TickDirection(row["tick_direction_alt"]),
            up_volume=int(row["up_volume"]) if row["up_volume"] is not None else None,
            down_volume=int(row["down_volume"]) if row["down_volume"] is not None else None,
            volume_split_method=VolumeSplitMethod(row["volume_split_method"]),
            source=row["source"],
            ingested_at_utc=datetime.fromisoformat(row["ingested_at_utc"])
                if row["ingested_at_utc"] else None,
        )

    # ---------- 매수/매도 강도 시리즈 (Buy-Sell Intensity Series) ----------

    def fetch_buy_sell_intensity(
        self,
        symbol: str,
        timeframe: Timeframe,
        start_utc: datetime,
        end_utc: datetime,
    ) -> list[tuple[datetime, Optional[Decimal], VolumeSplitMethod]]:
        """
        매수 강도 시리즈 조회.
        (Buy-sell intensity series.)

        Returns:
            List of (bar_time_utc, intensity_0_to_1_or_None, split_method)
            intensity = up / (up + down). 분류 없으면 None.
            split_method를 함께 반환 → 호출자가 신뢰도 판단.
        """
        bars = self.fetch(symbol, timeframe, start_utc, end_utc)
        result = []
        for b in bars:
            intensity = b.buy_sell_intensity()
            result.append((b.bar_time_utc, intensity, b.volume_split_method))
        return result

    # ---------- 누적 거래량 델타 (CVD) ----------

    def fetch_cumulative_volume_delta(
        self,
        symbol: str,
        timeframe: Timeframe,
        start_utc: datetime,
        end_utc: datetime,
    ) -> list[tuple[datetime, int, VolumeSplitMethod]]:
        """
        누적 거래량 델타 (CVD) 시리즈.
        (Cumulative Volume Delta series.)

        CVD[i] = Σ_{j<=i} (up_volume[j] - down_volume[j])

        분류 없는 봉은 0 기여로 취급 (skip cumulative impact).

        Returns:
            List of (bar_time_utc, cvd_value, split_method_at_bar)
        """
        bars = self.fetch(symbol, timeframe, start_utc, end_utc)
        result = []
        cvd = 0
        for b in bars:
            if b.up_volume is not None and b.down_volume is not None:
                cvd += (b.up_volume - b.down_volume)
            result.append((b.bar_time_utc, cvd, b.volume_split_method))
        return result

    # ---------- 갭 검출 (Gap Detection) ----------

    def detect_gaps(
        self,
        symbol: str,
        timeframe: Timeframe,
        start_utc: datetime,
        end_utc: datetime,
    ) -> list[tuple[datetime, datetime]]:
        """
        누락된 봉 구간 검출 (정확한 expected 시간은 캘린더 의존).
        (Detect missing bar ranges — expected times depend on calendar.)

        v0.1: 단순히 인접 봉 간 시간 차이가 timeframe보다 크면 gap으로 보고.
        (Simple version: report when adjacent bars exceed timeframe delta.)

        Returns:
            List of (gap_start_utc, gap_end_utc) — 누락된 구간.
        """
        bars = self.fetch(symbol, timeframe, start_utc, end_utc)
        if len(bars) < 2:
            return []

        from .dummy_source import _TIMEFRAME_DELTAS  # 재사용
        expected_delta = _TIMEFRAME_DELTAS[timeframe]

        gaps = []
        for i in range(len(bars) - 1):
            actual = bars[i + 1].bar_time_utc - bars[i].bar_time_utc
            # 1.5x 허용 (캘린더 휴장 등은 호출자가 별도 처리)
            if actual > expected_delta * 1.5:
                gaps.append((bars[i].bar_time_utc, bars[i + 1].bar_time_utc))
        return gaps
