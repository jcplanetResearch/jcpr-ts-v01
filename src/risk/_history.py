"""OrderHistory — 빈도/중복/whipsaw 검사를 위한 최근 주문 이력.

세션 동안의 in-memory 이력. 세션 종료 시 폐기 (장기 보관은 Task 25 ledger).
스레드 안전.

설계:
- deque(maxlen) 으로 메모리 상한 보장
- 윈도우 검사는 시간 기반 (now - window_seconds)
- side enum 비교로 whipsaw guard
"""
from __future__ import annotations
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from src.brokers import OrderIntent, Side


@dataclass(frozen=True)
class OrderRecord:
    """주문 이력 1건."""
    intent: OrderIntent
    submitted_at: datetime  # UTC tz-aware


class OrderHistory:
    """최근 주문 이력 — 윈도우 기반 빈도/중복 검사용."""

    DEFAULT_MAX_RECORDS = 10_000

    def __init__(self, max_records: int = DEFAULT_MAX_RECORDS) -> None:
        if max_records <= 0:
            raise ValueError("max_records must be positive")
        self._records: deque[OrderRecord] = deque(maxlen=max_records)
        self._lock = threading.Lock()

    def add(self, intent: OrderIntent, submitted_at: datetime) -> None:
        """주문 이력 추가. submitted_at 은 UTC tz-aware 여야 한다."""
        if submitted_at.tzinfo is None:
            raise ValueError("submitted_at must be tz-aware UTC")
        with self._lock:
            self._records.append(OrderRecord(intent, submitted_at))

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)

    def count_in_window(self, now: datetime, window_seconds: int) -> int:
        """now 부터 거꾸로 window_seconds 이내 전체 주문 수."""
        if window_seconds <= 0:
            return 0
        threshold = now - timedelta(seconds=window_seconds)
        with self._lock:
            return sum(1 for r in self._records if r.submitted_at >= threshold)

    def has_duplicate(
        self,
        intent: OrderIntent,
        now: datetime,
        window_seconds: int,
    ) -> bool:
        """동일 심볼·동일 사이드 주문이 윈도우 내에 있는가?

        risk_limits.yaml §7.1 duplicate_definition: same_symbol_same_side_within_window.
        """
        if window_seconds <= 0:
            return False
        threshold = now - timedelta(seconds=window_seconds)
        with self._lock:
            return any(
                r.intent.symbol == intent.symbol
                and r.intent.side == intent.side
                and r.submitted_at >= threshold
                for r in self._records
            )

    def has_recent_opposite_side(
        self,
        intent: OrderIntent,
        now: datetime,
        cooldown_seconds: int,
    ) -> bool:
        """반대 방향 주문이 cooldown 내에 있는가? (whipsaw guard).

        risk_limits.yaml §7.4 no_immediate_reversal.
        """
        if cooldown_seconds <= 0:
            return False
        threshold = now - timedelta(seconds=cooldown_seconds)
        opposite = Side.SELL if intent.side == Side.BUY else Side.BUY
        with self._lock:
            return any(
                r.intent.symbol == intent.symbol
                and r.intent.side == opposite
                and r.submitted_at >= threshold
                for r in self._records
            )

    def last_for_symbol(
        self, symbol: str
    ) -> Optional[OrderRecord]:
        """해당 심볼의 가장 최근 주문 (없으면 None)."""
        with self._lock:
            for r in reversed(self._records):
                if r.intent.symbol == symbol:
                    return r
        return None
