"""
시그널 스키마 v2 (Signal Schema v2)
====================================

JCPR Trading System - jcpr-ts-v01
Task 14 v0.4 / Task 15 호환

다중 신호 합성 (multi-factor confluence) 시그널 출력.
(Multi-factor confluence signal output.)

원칙 (Principles):
- 모든 datetime UTC tz-aware
- composite_score: -1 ~ +1 (정규화 보장)
- confidence: 0 ~ 1 (개별 신호 일치도)
- components: 감사 추적용 — 모든 개별 지표 값 보존
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Optional


class SignalSide(str, Enum):
    """시그널 방향 (Signal side)."""
    BUY = "buy"
    SELL = "sell"
    FLAT = "flat"   # 신호 없음/약함


@dataclass(frozen=True)
class MomentumSignalV04:
    """
    Task 14 v0.4 모멘텀 시그널.
    (Task 14 v0.4 momentum signal.)

    Fields:
        symbol: KRX 종목 코드
        timestamp_utc: 시그널 생성 시각 (UTC)
        composite_score: 합성 점수 (-1 ~ +1)
        side: buy / sell / flat
        confidence: 개별 신호 일치도 (0 ~ 1)
        components: 개별 지표 값 (감사 추적)
        metadata: 신뢰도/신선도 등 부가 정보
    """
    symbol: str
    timestamp_utc: datetime
    composite_score: Decimal
    side: SignalSide
    confidence: Decimal
    components: dict[str, Decimal] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    strategy_id: str = "momentum_v04"

    def __post_init__(self) -> None:
        if self.timestamp_utc.tzinfo is None:
            raise ValueError("timestamp_utc tz-aware 필수")
        if not (Decimal("-1") <= self.composite_score <= Decimal("1")):
            raise ValueError(
                f"composite_score 범위 [-1, 1] 위반: {self.composite_score}"
            )
        if not (Decimal("0") <= self.confidence <= Decimal("1")):
            raise ValueError(f"confidence 범위 [0, 1] 위반: {self.confidence}")
        if not self.symbol:
            raise ValueError("symbol 누락")

    def is_actionable(self, min_confidence: Decimal = Decimal("0.5")) -> bool:
        """주문 행동 가능한 시그널인지."""
        return self.side != SignalSide.FLAT and self.confidence >= min_confidence
