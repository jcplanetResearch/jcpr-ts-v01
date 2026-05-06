"""
거래량 매수/매도 분류기 (Volume Buy/Sell Classifier)
=====================================================

JCPR Trading System - jcpr-ts-v01
Task 12 v0.1

봉(bar) 데이터를 매수 주도/매도 주도로 분류.
(Classifies bar volume as buy-driven vs sell-driven.)

분류 방식 (Methods):
1. SOURCE_PROVIDED: 데이터 소스가 직접 매수/매도 체결량 제공 (KIS API 일부 응답)
2. ESTIMATED_HYBRID (기본 / default):
   - 직전 봉 종가 vs 현재 봉 종가 → 방향 결정
   - intra-bar 종가 위치 (close - low) / (high - low) → 강도 가중
3. ESTIMATED_SIMPLE: close vs prev_close만, 고정 비율 (0.7/0.3/0.5)
4. ESTIMATED_INTRABAR: intra-bar pressure만 (직전 봉 없을 때)

원칙 (Principles):
- 추정값임을 항상 명시 (volume_split_method 필드)
- fail-closed: 분류 불가 시 None 반환 (호출자가 명시적 처리)
- 결정론적 (deterministic): 같은 입력 → 같은 출력 (테스트 재현성)
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional, Tuple

from .ohlcv_schema import TickDirection, VolumeSplitMethod


# 하이브리드 추정에 사용되는 상수 (Constants for hybrid estimation)
# - 방향 가중치 (direction weight): 0.7 = 상승봉이면 70%를 매수로 추정
# - intra-bar 가중치: 종가 위치를 어느 정도 반영할지
HYBRID_DIRECTION_WEIGHT = Decimal("0.6")   # 직전 종가 비교 결과의 영향력
HYBRID_INTRABAR_WEIGHT = Decimal("0.4")    # intra-bar pressure의 영향력
# (합 = 1.0 — 두 신호의 가중 평균으로 매수 비율 산정)

SIMPLE_UP_RATIO = Decimal("0.7")    # 단순 추정 — 상승봉
SIMPLE_DOWN_RATIO = Decimal("0.3")  # 단순 추정 — 하락봉
NEUTRAL_RATIO = Decimal("0.5")      # 동가/불명확


# ─────────────────────────────────────────────────
# Tick Direction 분류 (단순 close vs prev_close)
# ─────────────────────────────────────────────────

def classify_tick_direction(
    close: Decimal,
    prev_close: Optional[Decimal],
) -> TickDirection:
    """
    직전 봉 종가 대비 분류 (Lee-Ready 단순화).
    (Classify based on previous close — simplified Lee-Ready.)
    """
    if prev_close is None:
        return TickDirection.UNKNOWN
    if close > prev_close:
        return TickDirection.UP
    if close < prev_close:
        return TickDirection.DOWN
    return TickDirection.ZERO


# ─────────────────────────────────────────────────
# Up/Down Volume 추정
# ─────────────────────────────────────────────────

def _intra_bar_pressure(
    high: Decimal, low: Decimal, close: Decimal,
) -> Decimal:
    """
    봉 내부 종가 위치 (0.0 = 저점, 1.0 = 고점).
    high == low이면 0.5 (중립).
    """
    if high == low:
        return NEUTRAL_RATIO
    pressure = (close - low) / (high - low)
    # 클램핑 (0~1 보장)
    if pressure < 0:
        return Decimal("0")
    if pressure > 1:
        return Decimal("1")
    return pressure


def estimate_up_down_volume_hybrid(
    open_: Decimal,
    high: Decimal,
    low: Decimal,
    close: Decimal,
    volume: int,
    prev_close: Optional[Decimal],
) -> Tuple[Optional[int], Optional[int], VolumeSplitMethod]:
    """
    하이브리드 추정 (기본 방식).
    (Hybrid estimation — default method.)

    1. 직전 종가 비교 → 방향 비율 (up_ratio_dir)
    2. intra-bar 종가 위치 → intra-bar 비율 (up_ratio_intra)
    3. 가중 평균: up_ratio = w1*dir + w2*intra
    4. up_volume = volume * up_ratio
    5. down_volume = volume - up_volume

    Returns:
        (up_volume, down_volume, method)
        volume == 0 이면 (0, 0, ESTIMATED_HYBRID)
        직전 종가 없으면 intra-bar만 사용 → ESTIMATED_INTRABAR로 변경
    """
    if volume == 0:
        return 0, 0, VolumeSplitMethod.ESTIMATED_HYBRID

    intra_ratio = _intra_bar_pressure(high, low, close)

    if prev_close is None:
        # 직전 봉 없음 → intra-bar만 사용
        up_volume = int(Decimal(volume) * intra_ratio)
        down_volume = volume - up_volume
        return up_volume, down_volume, VolumeSplitMethod.ESTIMATED_INTRABAR

    # 방향 비율
    if close > prev_close:
        dir_ratio = SIMPLE_UP_RATIO
    elif close < prev_close:
        dir_ratio = SIMPLE_DOWN_RATIO
    else:
        dir_ratio = NEUTRAL_RATIO

    # 가중 평균
    up_ratio = HYBRID_DIRECTION_WEIGHT * dir_ratio + HYBRID_INTRABAR_WEIGHT * intra_ratio
    # 클램핑 (0~1)
    if up_ratio < 0:
        up_ratio = Decimal("0")
    elif up_ratio > 1:
        up_ratio = Decimal("1")

    up_volume = int(Decimal(volume) * up_ratio)
    # 보정: int 변환 후 합이 정확히 volume이 되도록
    down_volume = volume - up_volume
    if down_volume < 0:
        # 부동소수점 보정 — up이 volume보다 약간 큰 경우
        up_volume = volume
        down_volume = 0

    return up_volume, down_volume, VolumeSplitMethod.ESTIMATED_HYBRID


def estimate_up_down_volume_simple(
    close: Decimal,
    volume: int,
    prev_close: Optional[Decimal],
) -> Tuple[Optional[int], Optional[int], VolumeSplitMethod]:
    """단순 추정 — close vs prev_close만 (intra-bar 무시)."""
    if volume == 0:
        return 0, 0, VolumeSplitMethod.ESTIMATED_SIMPLE
    if prev_close is None:
        return None, None, VolumeSplitMethod.UNKNOWN

    if close > prev_close:
        ratio = SIMPLE_UP_RATIO
    elif close < prev_close:
        ratio = SIMPLE_DOWN_RATIO
    else:
        ratio = NEUTRAL_RATIO

    up_volume = int(Decimal(volume) * ratio)
    down_volume = volume - up_volume
    return up_volume, down_volume, VolumeSplitMethod.ESTIMATED_SIMPLE


# ─────────────────────────────────────────────────
# 통합 분류 함수 (Unified Classification)
# ─────────────────────────────────────────────────

def classify_bar(
    open_: Decimal,
    high: Decimal,
    low: Decimal,
    close: Decimal,
    volume: int,
    prev_close: Optional[Decimal],
    *,
    method: str = "hybrid",
) -> Tuple[TickDirection, Optional[int], Optional[int], VolumeSplitMethod]:
    """
    봉 1개를 분류.
    (Classify one bar.)

    Returns:
        (tick_direction, up_volume, down_volume, split_method)
    """
    direction = classify_tick_direction(close, prev_close)

    if method == "hybrid":
        up, down, split = estimate_up_down_volume_hybrid(
            open_, high, low, close, volume, prev_close,
        )
    elif method == "simple":
        up, down, split = estimate_up_down_volume_simple(close, volume, prev_close)
    elif method == "intrabar":
        if volume == 0:
            up, down, split = 0, 0, VolumeSplitMethod.ESTIMATED_INTRABAR
        else:
            ratio = _intra_bar_pressure(high, low, close)
            up = int(Decimal(volume) * ratio)
            down = volume - up
            split = VolumeSplitMethod.ESTIMATED_INTRABAR
    else:
        raise ValueError(f"알 수 없는 분류 방식 (unknown method): {method}")

    return direction, up, down, split
