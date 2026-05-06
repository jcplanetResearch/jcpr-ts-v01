"""
OHLCV 데이터 스키마 (OHLCV Data Schema)
========================================

JCPR Trading System - jcpr-ts-v01
Task 12 v0.1 데이터 모델

봉(bar) 데이터 모델 + 매수/매도 분류 필드.
(Bar data model + buy/sell classification fields.)

원칙 (Principles):
- UTC tz-aware datetime 강제 (요구사항)
- Decimal 가격 (정밀도 보존)
- frozen=True (로드 후 수정 불가)
- fail-closed: 검증 실패는 예외
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional


# ─────────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────────

class Timeframe(str, Enum):
    """봉 시간 단위 (Bar timeframe)."""
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    M60 = "60m"
    D1 = "1d"


class TickDirection(str, Enum):
    """
    봉의 직전 봉 종가 대비 방향.
    (Bar direction vs previous bar's close.)

    Lee-Ready 알고리즘의 단순화 (Simplified Lee-Ready):
    - UP:      close > prev_close  → 매수 우세 추정
    - DOWN:    close < prev_close  → 매도 우세 추정
    - ZERO:    close == prev_close → 중립
    - UNKNOWN: 직전 봉 없음 또는 분류 불가
    """
    UP = "up"
    DOWN = "down"
    ZERO = "zero"
    UNKNOWN = "unknown"


class VolumeSplitMethod(str, Enum):
    """
    up_volume/down_volume 분할 방법.
    (Method used to split volume into up/down.)

    호출자가 신뢰도를 판단할 수 있도록 명시.
    (Caller can judge reliability.)
    """
    SOURCE_PROVIDED = "source_provided"          # 데이터 소스가 직접 제공 (가장 신뢰)
    ESTIMATED_HYBRID = "estimated_hybrid"        # 직전 종가 + intra-bar 결합 추정
    ESTIMATED_SIMPLE = "estimated_simple"        # close vs prev_close만
    ESTIMATED_INTRABAR = "estimated_intrabar"    # intra-bar pressure만
    UNKNOWN = "unknown"                          # 분할 불가


# ─────────────────────────────────────────────────
# OHLCV Bar 데이터 모델
# ─────────────────────────────────────────────────

@dataclass(frozen=True)
class OHLCVBar:
    """
    OHLCV 봉 1개.
    (Single OHLCV bar.)

    매수/매도 분류 필드 (Buy/sell classification fields):
    - tick_direction:     주 분류 (계산/소스 제공)
    - tick_direction_alt: 대체 출처용 — 향후 다른 데이터 소스나 다른 알고리즘
                         (alternative source — for future cross-validation)
    - up_volume / down_volume: 분류된 거래량 (None = 미분류)
    - volume_split_method: 분할 방법 (신뢰도 판단용)
    """
    # 식별자 (PK)
    symbol: str
    timeframe: Timeframe
    bar_time_utc: datetime

    # OHLCV
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int

    # 선택 필드
    value_krw: Optional[Decimal] = None  # 거래대금 (있을 경우)

    # ★ 매수/매도 분류 필드 (Buy/Sell classification — Task 12 v0.1)
    tick_direction: TickDirection = TickDirection.UNKNOWN
    tick_direction_alt: TickDirection = TickDirection.UNKNOWN  # ★ 대체 출처
    up_volume: Optional[int] = None
    down_volume: Optional[int] = None
    volume_split_method: VolumeSplitMethod = VolumeSplitMethod.UNKNOWN

    # 메타
    source: str = "unknown"
    ingested_at_utc: Optional[datetime] = None

    def __post_init__(self) -> None:
        # 1) tz-aware 검증
        if self.bar_time_utc.tzinfo is None:
            raise ValueError(
                f"bar_time_utc는 tz-aware여야 함 (must be tz-aware): {self.bar_time_utc}"
            )
        if self.ingested_at_utc is not None and self.ingested_at_utc.tzinfo is None:
            raise ValueError("ingested_at_utc는 tz-aware여야 함")

        # 2) 가격 양수 검증
        for name in ("open", "high", "low", "close"):
            v = getattr(self, name)
            if v <= 0:
                raise ValueError(f"{name}은 양수여야 함: {v}")

        # 3) OHLC 정합성 (low ≤ open/close ≤ high)
        if not (self.low <= self.open <= self.high):
            raise ValueError(
                f"OHLC 정합성 위반: low={self.low} ≤ open={self.open} ≤ high={self.high}"
            )
        if not (self.low <= self.close <= self.high):
            raise ValueError(
                f"OHLC 정합성 위반: low={self.low} ≤ close={self.close} ≤ high={self.high}"
            )

        # 4) 거래량 음수 불가
        if self.volume < 0:
            raise ValueError(f"volume은 음수 불가: {self.volume}")

        # 5) up/down volume 검증
        if self.up_volume is not None and self.up_volume < 0:
            raise ValueError(f"up_volume 음수 불가: {self.up_volume}")
        if self.down_volume is not None and self.down_volume < 0:
            raise ValueError(f"down_volume 음수 불가: {self.down_volume}")

        # 6) up + down ≤ total (둘 다 있을 때)
        if self.up_volume is not None and self.down_volume is not None:
            if self.up_volume + self.down_volume > self.volume:
                raise ValueError(
                    f"up_volume({self.up_volume}) + down_volume({self.down_volume}) "
                    f"> volume({self.volume})"
                )

        # 7) value_krw 검증
        if self.value_krw is not None and self.value_krw < 0:
            raise ValueError(f"value_krw 음수 불가: {self.value_krw}")

    # ---------- 파생 지표 (Derived Metrics) ----------

    def buy_sell_imbalance(self) -> Optional[Decimal]:
        """
        매수/매도 불균형 (-1.0 ~ +1.0).
        +1.0 = 100% 매수, -1.0 = 100% 매도, 0 = 균형.
        분류 없거나 volume=0이면 None.
        """
        if self.up_volume is None or self.down_volume is None or self.volume == 0:
            return None
        return (Decimal(self.up_volume) - Decimal(self.down_volume)) / Decimal(self.volume)

    def buy_sell_intensity(self) -> Optional[Decimal]:
        """
        매수 강도 (0.0 ~ 1.0). up / (up + down).
        분류 없거나 (up+down)=0이면 None.
        """
        if self.up_volume is None or self.down_volume is None:
            return None
        total = self.up_volume + self.down_volume
        if total == 0:
            return None
        return Decimal(self.up_volume) / Decimal(total)

    def intra_bar_pressure(self) -> Optional[Decimal]:
        """
        봉 내부 종가 위치 (0.0 ~ 1.0). (close - low) / (high - low).
        high == low면 None.
        """
        if self.high == self.low:
            return None
        return (self.close - self.low) / (self.high - self.low)
