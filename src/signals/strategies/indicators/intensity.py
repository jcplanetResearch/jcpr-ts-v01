"""
매수/매도 강도 집계 (Buy-Sell Intensity Aggregation)
=====================================================

Task 12 OHLCVStore의 buy_sell_intensity()를 N봉 평균 → [-1, +1] 매핑.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional, Sequence


def compute_buy_sell_intensity(
    intensities: Sequence[Optional[Decimal]],
    lookback: int,
) -> Optional[Decimal]:
    """
    최근 N봉의 평균 매수 강도 → [-1, +1] 매핑.
    
    intensities[i]:
        - None: 분류 불가 (skip)
        - 0~1: up_volume / (up_volume + down_volume)
    
    매핑:
        - 평균 0.5: 0 (균형)
        - 평균 1.0: +1 (전적 매수)
        - 평균 0.0: -1 (전적 매도)
    
    유효 데이터가 lookback의 50% 미만이면 None.
    """
    if lookback <= 0:
        raise ValueError(f"lookback 양수: {lookback}")
    if len(intensities) < lookback:
        return None
    
    recent = intensities[-lookback:]
    valid = [v for v in recent if v is not None]
    
    if len(valid) < lookback // 2:  # 50% 미만은 신뢰 불가
        return None
    
    avg = sum(valid) / Decimal(len(valid))
    # [0, 1] → [-1, +1]
    return (avg - Decimal("0.5")) * Decimal("2")
