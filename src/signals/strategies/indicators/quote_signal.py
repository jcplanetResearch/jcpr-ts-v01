"""
호가 기반 지표 (Quote-Based Indicators)
========================================

Task 13 QuoteSnapshot 활용:
- imbalance: 매수/매도 잔량 불균형 [-1, +1]
- spread_quality: 좁을수록 +1 (체결성/유동성 양호)
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional


def compute_quote_imbalance(snap) -> Optional[Decimal]:
    """
    호가 imbalance [-1, +1].
    snap.imbalance() 또는 snap.depth_imbalance() 활용.
    
    snap이 None이거나 imbalance 계산 불가면 None.
    """
    if snap is None:
        return None
    # depth가 있으면 depth_imbalance(5) 우선, 없으면 best level imbalance
    if snap.depth_levels:
        di = snap.depth_imbalance(levels=5)
        if di is not None:
            return di
    return snap.imbalance()


def compute_spread_quality(
    snap,
    *,
    max_acceptable_bps: Decimal = Decimal("100"),  # 1% 이상 스프레드는 매우 안 좋음
) -> Optional[Decimal]:
    """
    스프레드 품질 [0, +1].
    
    스프레드가 좁을수록 +1, 클수록 0.
    
    매핑:
        - spread_bps == 0: +1
        - spread_bps == max_acceptable_bps: 0
        - spread_bps > max: 0 (clamp)
    
    부호는 항상 양수 — composite_score에서는 방향 신호와 곱하지 않고
    confidence/품질 가중으로만 사용.
    """
    if snap is None:
        return None
    bps = snap.spread_bps()
    if bps is None:
        return None
    if bps <= 0:
        return Decimal("1")
    if bps >= max_acceptable_bps:
        return Decimal("0")
    quality = Decimal("1") - (bps / max_acceptable_bps)
    return quality
