"""중복 주문 방지 게이트 (Duplicate Order Detection Gate)."""

from __future__ import annotations

from .base import GateResult, RiskContext, RiskGate


class DuplicateOrderGate(RiskGate):
    """
    동일 (symbol, side) 미체결 주문이 이미 존재하면 신규 주문 거부.
    (Reject new order if same (symbol, side) is already pending.)
    """

    name = "duplicate_order"

    def evaluate(self, ctx: RiskContext) -> GateResult:
        for order in ctx.pending_orders:
            if (
                order.get("symbol") == ctx.symbol
                and order.get("side") == ctx.side
                and order.get("status") in ("pending", "submitted", "partial_fill")
            ):
                return GateResult(
                    gate_name=self.name, outcome="reject",
                    reason=f"동일 의도 미체결 주문 존재 (duplicate pending order): "
                           f"{ctx.symbol}/{ctx.side}",
                    detail={
                        "existing_order_id": order.get("order_id"),
                        "status": order.get("status"),
                    },
                )
        return GateResult(gate_name=self.name, outcome="pass", reason=None)
