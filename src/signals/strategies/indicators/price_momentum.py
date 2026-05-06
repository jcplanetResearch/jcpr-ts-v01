"""
가격 모멘텀 (Price Momentum)
=============================

(close[-1] - close[-N]) / close[-N] → 정규화 → [-1, +1]
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional, Sequence

# 정규화 임계 — 이 값을 초과하는 모멘텀은 ±1로 클램핑
# 일봉 기준 ±10%를 강한 모멘텀으로 간주
DEFAULT_NORMALIZATION_THRESHOLD = Decimal("0.10")


def compute_price_momentum(
    closes: Sequence[Decimal],
    lookback: int,
    *,
    normalization_threshold: Decimal = DEFAULT_NORMALIZATION_THRESHOLD,
) -> Optional[Decimal]:
    """
    가격 모멘텀 [-1, +1].
    
    Args:
        closes: 종가 시리즈 (시간 오름차순)
        lookback: 비교할 과거 봉 수
    
    Returns:
        [-1, +1] 정규화된 모멘텀, 데이터 부족 시 None.
    """
    if lookback <= 0:
        raise ValueError(f"lookback 양수: {lookback}")
    if len(closes) < lookback + 1:
        return None
    
    past = closes[-lookback - 1]
    now = closes[-1]
    if past <= 0:
        return None
    
    raw = (now - past) / past  # -1 ~ + 무한 가능
    
    # 정규화 — threshold 기준 [-1, +1] 클램핑
    normalized = raw / normalization_threshold
    if normalized > Decimal("1"):
        return Decimal("1")
    if normalized < Decimal("-1"):
        return Decimal("-1")
    return normalized
