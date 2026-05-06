"""
더미 호가 소스 (Dummy Quote Source)
====================================

JCPR Trading System - jcpr-ts-v01
Task 13 v0.1

결정론적 합성 호가 + KRX 호가단위 정합.
(Deterministic synthetic quotes with KRX tick alignment.)

⚠️ 절대 실거래 사용 금지 (NEVER use for live trading) — is_live=False.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from .quote_schema import DEFAULT_DEPTH_LEVELS, DepthLevel, QuoteSnapshot
from .quote_source import QuoteSource

# Task 18 호가단위 헬퍼 재사용 (Reuse from Task 18)
from ..execution.tick_size import get_tick_size


def _seeded_random(seed_str: str) -> float:
    h = hashlib.sha256(seed_str.encode("utf-8")).hexdigest()
    return int(h[:8], 16) / 0xFFFFFFFF


class DummyQuoteSource(QuoteSource):
    """
    합성 호가 생성기.
    (Synthetic quote generator.)

    같은 (symbol, time) → 같은 스냅샷 (테스트 재현성).
    """

    name = "dummy_quote"

    def __init__(
        self,
        base_price: Decimal = Decimal("70000"),
        depth_levels: int = DEFAULT_DEPTH_LEVELS,
        instrument_type: str = "stock",
    ):
        if depth_levels < 1:
            raise ValueError("depth_levels >= 1")
        self._base = base_price
        self._depth_levels = depth_levels
        self._instrument_type = instrument_type

    @property
    def is_live(self) -> bool:
        return False

    def snapshot(self, symbol: str, *, fixed_time: Optional[datetime] = None) -> QuoteSnapshot:
        """
        호가 스냅샷 생성.
        (Create quote snapshot.)

        fixed_time: 결정론 테스트용 — 지정 시 해당 시각 기준 생성
        """
        if not symbol:
            raise ValueError("symbol 필수")

        now = fixed_time if fixed_time is not None else datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError("fixed_time tz-aware 필수")

        # 결정론적 base price 변동 (-2% ~ +2%)
        seed = f"{symbol}:{now.replace(microsecond=0).isoformat()}"
        rnd1 = _seeded_random(seed + ":mid")
        rnd2 = _seeded_random(seed + ":imb")

        change_pct = Decimal(str((rnd1 - 0.5) * 0.04))
        mid_raw = self._base * (Decimal("1") + change_pct)

        # 호가단위 정합 — mid를 tick에 맞춤
        tick = Decimal(get_tick_size(mid_raw, self._instrument_type))
        mid = (mid_raw // tick) * tick
        if mid <= 0:
            mid = tick

        # best_bid = mid - tick (1단계 아래), best_ask = mid + tick (1단계 위)
        # 단, mid 자체가 호가에 정합하므로:
        best_bid = mid
        best_ask = mid + tick

        # 매수/매도 잔량 (불균형 변수로 변동)
        imb_skew = (rnd2 - 0.5) * 0.4  # ±20% 비율 변동
        bid_base = 1000
        ask_base = 1000
        best_bid_size = int(bid_base * (1 + imb_skew))
        best_ask_size = int(ask_base * (1 - imb_skew))
        if best_bid_size < 0:
            best_bid_size = 0
        if best_ask_size < 0:
            best_ask_size = 0

        # 호가창 (depth) 생성 — KRX는 매수/매도가 분리되어 있으나
        # 단순화를 위해 각 level에 ask_price = mid + level*tick, bid_price = mid - (level-1)*tick 가정
        # 여기서는 통합 표현: level 1 = best, level N = N단계 떨어진 곳
        depth: list[DepthLevel] = []
        for lvl in range(1, self._depth_levels + 1):
            # 가격은 매수/매도 평균을 그 단계의 가격으로 (단순화)
            # 실제 KRX는 bid_prices[lvl] = best_bid - (lvl-1)*tick, ask_prices[lvl] = best_ask + (lvl-1)*tick
            level_price = mid + Decimal(lvl - 1) * tick
            # 잔량은 단계가 멀수록 감소
            decay = Decimal("0.85") ** (lvl - 1)
            bid_sz = int(Decimal(best_bid_size) * decay)
            ask_sz = int(Decimal(best_ask_size) * decay)
            depth.append(DepthLevel(
                level=lvl,
                price=level_price,
                bid_size=bid_sz,
                ask_size=ask_sz,
            ))

        return QuoteSnapshot(
            symbol=symbol,
            captured_at_utc=now,
            received_at_utc=now,
            best_bid=best_bid,
            best_ask=best_ask,
            best_bid_size=best_bid_size,
            best_ask_size=best_ask_size,
            depth_levels=tuple(depth),
            last_trade_price=mid,
            source=self.name,
            is_live_source=False,
        )
