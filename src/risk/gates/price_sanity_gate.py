"""가격 합리성 점검 게이트 (Price Sanity Gate)."""

from __future__ import annotations

from decimal import Decimal

from .base import GateResult, RiskContext, RiskGate


class PriceSanityGate(RiskGate):
    """
    지정가가 직전 체결가/현재가 대비 ±X% 벗어나면 거부.
    (Reject limit price if it deviates from last quote by ±X%.)
    """

    name = "price_sanity"

    def __init__(self, max_deviation_pct: Decimal = Decimal("0.05")):
        if max_deviation_pct <= 0 or max_deviation_pct > 1:
            raise ValueError("max_deviation_pct는 (0,1] 범위")
        self._max_dev = max_deviation_pct

    def evaluate(self, ctx: RiskContext) -> GateResult:
        if ctx.last_quote_price is None:
            return GateResult(
                gate_name=self.name, outcome="reject",
                reason="기준가 없음 (no reference quote, fail-closed)",
                detail={"symbol": ctx.symbol},
            )
        if ctx.last_quote_price <= 0:
            return GateResult(
                gate_name=self.name, outcome="reject",
                reason=f"기준가 비정상 (invalid reference quote): {ctx.last_quote_price}",
            )

        deviation = abs(ctx.price - ctx.last_quote_price) / ctx.last_quote_price
        if deviation > self._max_dev:
            return GateResult(
                gate_name=self.name, outcome="reject",
                reason=f"가격 편차 한도 초과 (price deviation exceeded): "
                       f"{deviation:.4%} > {self._max_dev:.4%}",
                detail={
                    "order_price": str(ctx.price),
                    "ref_price": str(ctx.last_quote_price),
                    "deviation": f"{deviation:.6f}",
                    "max": f"{self._max_dev:.6f}",
                },
            )
        return GateResult(gate_name=self.name, outcome="pass", reason=None)
