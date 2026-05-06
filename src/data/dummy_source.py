"""
더미 시장 데이터 소스 (Dummy Market Data Source)
=================================================

JCPR Trading System - jcpr-ts-v01
Task 12 v0.1

테스트/오프라인용 결정론적 합성 데이터 생성.
(Deterministic synthetic data generator for testing/offline.)

⚠️ 절대 실거래에 사용 금지 (NEVER use for live trading).
- is_live = False 명시
- 시그널 러너는 실거래 모드에서 거부해야 함
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Iterable, Optional

from .market_data_source import MarketDataSource
from .ohlcv_schema import OHLCVBar, Timeframe
from .volume_classifier import classify_bar


_TIMEFRAME_DELTAS = {
    Timeframe.M1: timedelta(minutes=1),
    Timeframe.M5: timedelta(minutes=5),
    Timeframe.M15: timedelta(minutes=15),
    Timeframe.M60: timedelta(hours=1),
    Timeframe.D1: timedelta(days=1),
}


def _seeded_random(seed_str: str) -> float:
    """결정론적 의사 난수 (0~1) — 시드 문자열의 SHA256 기반."""
    h = hashlib.sha256(seed_str.encode("utf-8")).hexdigest()
    return int(h[:8], 16) / 0xFFFFFFFF


class DummySource(MarketDataSource):
    """
    합성 OHLCV 생성기.
    (Synthetic OHLCV generator.)

    같은 (symbol, timeframe, time) → 같은 봉 (결정론적).
    """

    name = "dummy"

    def __init__(self, base_price: Decimal = Decimal("70000"), classify_method: str = "hybrid"):
        self._base = base_price
        self._classify_method = classify_method

    @property
    def is_live(self) -> bool:
        return False

    def fetch_bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        start_utc: datetime,
        end_utc: datetime,
    ) -> Iterable[OHLCVBar]:
        if start_utc.tzinfo is None or end_utc.tzinfo is None:
            raise ValueError("start_utc, end_utc는 tz-aware여야 함")
        if start_utc > end_utc:
            raise ValueError(f"start_utc > end_utc: {start_utc} > {end_utc}")

        delta = _TIMEFRAME_DELTAS[timeframe]
        bars: list[OHLCVBar] = []
        prev_close: Optional[Decimal] = None
        ingested = datetime.now(timezone.utc)

        current = start_utc
        idx = 0
        while current <= end_utc:
            # 결정론적 가격 생성 (-3% ~ +3% 변동)
            seed = f"{symbol}:{timeframe.value}:{current.isoformat()}"
            rnd1 = _seeded_random(seed + ":1")  # 0~1
            rnd2 = _seeded_random(seed + ":2")
            rnd3 = _seeded_random(seed + ":3")
            rnd4 = _seeded_random(seed + ":4")

            # 직전 봉 종가 또는 base price
            anchor = prev_close if prev_close is not None else self._base

            # 변동률 -3% ~ +3%
            change_pct = Decimal(str((rnd1 - 0.5) * 0.06))
            close = (anchor * (Decimal("1") + change_pct)).quantize(Decimal("1"))

            # high/low: close 주변 ±2%
            spread_high = Decimal(str(rnd2 * 0.02))
            spread_low = Decimal(str(rnd3 * 0.02))
            high = (close * (Decimal("1") + spread_high)).quantize(Decimal("1"))
            low = (close * (Decimal("1") - spread_low)).quantize(Decimal("1"))
            # 정합성 보정
            if high < close:
                high = close
            if low > close:
                low = close

            # open: low~high 사이
            open_ = (low + (high - low) * Decimal(str(rnd4))).quantize(Decimal("1"))
            if open_ < low:
                open_ = low
            if open_ > high:
                open_ = high

            # 가격 양수 보장
            if low <= 0:
                low = Decimal("1")
            if open_ <= 0:
                open_ = Decimal("1")
            if close <= 0:
                close = Decimal("1")
            if high <= 0:
                high = Decimal("1")
            # 재정합 보정
            if high < max(open_, close):
                high = max(open_, close)
            if low > min(open_, close):
                low = min(open_, close)

            volume = int(10000 + rnd1 * 90000)

            # 분류 적용 (Task 12 v0.1 핵심)
            tick_dir, up_vol, down_vol, split_method = classify_bar(
                open_, high, low, close, volume, prev_close,
                method=self._classify_method,
            )

            bar = OHLCVBar(
                symbol=symbol,
                timeframe=timeframe,
                bar_time_utc=current,
                open=open_, high=high, low=low, close=close,
                volume=volume,
                value_krw=close * Decimal(volume),
                tick_direction=tick_dir,
                tick_direction_alt=tick_dir,  # dummy에서는 동일 (실 어댑터에서 다른 알고리즘 사용 가능)
                up_volume=up_vol,
                down_volume=down_vol,
                volume_split_method=split_method,
                source=self.name,
                ingested_at_utc=ingested,
            )
            bars.append(bar)
            prev_close = close

            current = current + delta
            idx += 1
            if idx > 10000:  # 안전 가드
                raise RuntimeError("DummySource: 너무 많은 봉 요청 (>10000)")

        return bars
