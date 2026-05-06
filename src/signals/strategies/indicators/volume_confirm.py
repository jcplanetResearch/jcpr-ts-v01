"""
거래량 확인 (Volume Confirmation)
==================================

최근 평균 거래량 vs 장기 평균 거래량.
값 > 1 → 거래량 증가 → 모멘텀 신뢰도 ↑
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional, Sequence


def compute_volume_confirmation(
    volumes: Sequence[int],
    short_window: int,
    long_window: int,
    *,
    direction: int = 1,  # +1 (buy) or -1 (sell) — 같은 거래량 증가도 매도 시그널 강화 가능
) -> Optional[Decimal]:
    """
    거래량 확인 신호 [-1, +1].
    
    short_avg / long_avg 비율을 [-1, +1]로 매핑:
    - 비율 == 1: 0 (변화 없음)
    - 비율 ≥ 2: ±1 (거래량 2배 이상)
    - 비율 ≤ 0.5: -direction (거래량 절반 이하)
    
    direction에 따라 부호 결정:
    - 가격 상승 + 거래량 증가 → +
    - 가격 하락 + 거래량 증가 → - (매도 신호 강화)
    - 거래량 감소 → 0에 가까운 값 (모멘텀 약화)
    """
    if short_window <= 0 or long_window <= 0:
        raise ValueError("window 양수 필요")
    if short_window > long_window:
        raise ValueError("short_window <= long_window")
    if len(volumes) < long_window:
        return None
    if direction not in (1, -1):
        raise ValueError("direction은 +1 또는 -1")
    
    recent = volumes[-short_window:]
    longer = volumes[-long_window:]
    
    short_avg = Decimal(sum(recent)) / Decimal(short_window)
    long_avg = Decimal(sum(longer)) / Decimal(long_window)
    
    if long_avg == 0:
        return None
    
    ratio = short_avg / long_avg  # 1 = 변화 없음
    
    # ratio를 [-1, +1] 매핑
    # ratio = 1 → 0, ratio = 2 → +1, ratio = 0.5 → -1
    if ratio >= 2:
        score = Decimal("1")
    elif ratio <= Decimal("0.5"):
        score = Decimal("-1")
    elif ratio >= 1:
        # [1, 2] → [0, 1]
        score = ratio - Decimal("1")
    else:
        # [0.5, 1] → [-1, 0]
        score = (ratio - Decimal("1")) * Decimal("2")
    
    # direction 적용
    return score * Decimal(direction)
