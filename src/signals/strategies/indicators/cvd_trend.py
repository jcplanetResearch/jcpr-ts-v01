"""
CVD 트렌드 (Cumulative Volume Delta Trend)
===========================================

CVD 시리즈의 기울기 (slope) 또는 차분 기반 트렌드.
양의 기울기 → 매수 우위 누적, 음의 기울기 → 매도 우위.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional, Sequence


def compute_cvd_trend(
    cvd_series: Sequence[int],
    lookback: int,
    *,
    normalization_threshold: int = 100_000,
) -> Optional[Decimal]:
    """
    CVD 시리즈의 트렌드 [-1, +1].
    
    단순 차분: CVD[-1] - CVD[-lookback]
    normalization_threshold 기준 [-1, +1] 클램핑.
    
    Args:
        cvd_series: CVD 누적값 시리즈 (시간 오름차순)
        lookback: 비교 구간
        normalization_threshold: 이 절댓값 이상이면 ±1 (단위: 거래량)
    """
    if lookback <= 0:
        raise ValueError(f"lookback 양수: {lookback}")
    if normalization_threshold <= 0:
        raise ValueError("normalization_threshold 양수 필요")
    if len(cvd_series) < lookback + 1:
        return None
    
    delta = cvd_series[-1] - cvd_series[-lookback - 1]
    
    normalized = Decimal(delta) / Decimal(normalization_threshold)
    if normalized > Decimal("1"):
        return Decimal("1")
    if normalized < Decimal("-1"):
        return Decimal("-1")
    return normalized
