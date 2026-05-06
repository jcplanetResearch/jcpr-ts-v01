"""
단일 주문 크기 게이트 (Single Order Size Gate)
================================================

JCPR Trading System - jcpr-ts-v01
Task 47 v0.1

단일 주문 비용이 자본의 N% 초과 시 거부.
(Reject when single order cost exceeds N% of equity.)

기존 OrderSizer (Task 18)에도 max_pct_of_equity 한도가 있으나, 차이:
- Task 18 OrderSizer: 사이저가 size 계산 시 한도 내로 자르기 (또는 reject)
- Task 47 SingleOrderSizeGate: 게이트 단계에서 최종 검증
   (사이저가 우회되거나 외부에서 OrderIntent가 오는 경우 안전망)

원칙:
- BUY만 검사 (SELL은 pass)
- equity_krw <= 0 → reject
"""

from __future__ import annotations

from decimal import Decimal

from .base import GateResult, RiskContext, RiskGate


class SingleOrderSizeGate(RiskGate):
    """단일 주문 비용이 자본의 N% 초과 시 거부."""

    name = "single_order_size"

    def __init__(self, max_single_order_pct_of_equity: Decimal):
        if max_single_order_pct_of_equity <= 0 or max_single_order_pct_of_equity > 1:
            raise ValueError(
                f"max_single_order_pct_of_equity는 (0, 1] 범위: "
                f"{max_single_order_pct_of_equity}"
            )
        self._max_pct = max_single_order_pct_of_equity

    def evaluate(self, ctx: RiskContext) -> GateResult:
        if ctx.side == "sell":
            return GateResult(gate_name=self.name, outcome="pass", reason=None)

        if ctx.equity_krw <= 0:
            return GateResult(
                gate_name=self.name, outcome="reject",
                reason="자본 비정상 (equity invalid)",
                detail={"equity_krw": str(ctx.equity_krw)},
            )

        order_pct = ctx.estimated_cost_krw / ctx.equity_krw

        if order_pct > self._max_pct:
            return GateResult(
                gate_name=self.name, outcome="reject",
                reason=(
                    f"단일 주문 크기 한도 초과 "
                    f"(single order size exceeded): "
                    f"order_pct={order_pct:.4f} > max={self._max_pct:.4f}"
                ),
                detail={
                    "candidate_cost_krw": str(ctx.estimated_cost_krw),
                    "equity_krw": str(ctx.equity_krw),
                    "order_pct": f"{order_pct:.6f}",
                    "max_pct": f"{self._max_pct:.6f}",
                },
            )

        return GateResult(
            gate_name=self.name, outcome="pass", reason=None,
            detail={
                "order_pct": f"{order_pct:.6f}",
                "max_pct": f"{self._max_pct:.6f}",
            },
        )
