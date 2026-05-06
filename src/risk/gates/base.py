"""
리스크 게이트 기반 클래스 (Risk Gate Base)
==========================================

JCPR Trading System - jcpr-ts-v01
Task 19 v0.3 공통 모듈

모든 개별 게이트는 RiskGate를 상속하여 evaluate()를 구현.
(All individual gates inherit RiskGate and implement evaluate().)

원칙 (Principles):
- fail-closed: evaluate() 내부 예외 시 자동 reject (예외 = 통과 불가)
- stop-first: kill-switch 게이트가 항상 첫 번째로 평가됨
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal, Optional

logger = logging.getLogger(__name__)

GateOutcome = Literal["pass", "reject"]


@dataclass(frozen=True)
class RiskContext:
    """
    리스크 평가 시 전달되는 컨텍스트.
    (Context passed to risk evaluation.)
    """
    # 주문 의도
    symbol: str
    side: str                          # "buy" | "sell"
    quantity: int
    price: Decimal
    estimated_cost_krw: Decimal
    strategy_id: str
    intent_id: str                     # 주문 의도 고유 ID
    instrument_type: str

    # 계좌/포트폴리오 상태
    equity_krw: Decimal
    available_cash_krw: Decimal
    daily_realized_pnl_krw: Decimal
    open_positions: dict[str, dict[str, Any]]   # symbol -> position info
    pending_orders: list[dict[str, Any]]        # 미체결 주문 목록

    # 시장 상태
    market_now_utc: datetime
    market_is_open: bool
    last_quote_price: Optional[Decimal]         # 직전 체결가/현재가
    last_order_at_utc: Optional[datetime]       # 직전 주문 시각 (시스템 전체)
    last_order_for_symbol_utc: Optional[datetime]  # 동일 종목 직전 주문 시각


@dataclass(frozen=True)
class GateResult:
    """단일 게이트 평가 결과."""
    gate_name: str
    outcome: GateOutcome
    reason: Optional[str]
    detail: dict[str, Any] = field(default_factory=dict)


class RiskGate(ABC):
    """모든 리스크 게이트의 추상 베이스 클래스."""

    name: str = "unnamed_gate"

    @abstractmethod
    def evaluate(self, ctx: RiskContext) -> GateResult:
        """평가 수행. 통과면 pass, 아니면 reject 반환."""
        raise NotImplementedError

    def safe_evaluate(self, ctx: RiskContext) -> GateResult:
        """
        예외도 reject로 처리하는 안전 래퍼.
        (Safe wrapper that converts exceptions to reject — fail-closed.)
        """
        try:
            return self.evaluate(ctx)
        except Exception as e:  # noqa: BLE001 - intentional broad catch (fail-closed)
            logger.exception("게이트 예외 (gate exception) name=%s", self.name)
            return GateResult(
                gate_name=self.name,
                outcome="reject",
                reason=f"게이트 예외 발생 (gate exception): {type(e).__name__}: {e}",
                detail={"exception_type": type(e).__name__},
            )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
