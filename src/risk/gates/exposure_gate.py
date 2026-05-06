"""종목별 노출 한도 게이트 (Per-Symbol Exposure Gate)."""

from __future__ import annotations

from decimal import Decimal

from .base import GateResult, RiskContext, RiskGate


class ExposureGate(RiskGate):
    """
    단일 종목 평가액 비중이 자본의 N% 초과 시 추가 매수 거부.
    (Reject new buys when single-symbol exposure exceeds N% of equity.)
    """

    name = "exposure_per_symbol"

    def __init__(self, max_pct_per_symbol: Decimal):
        if max_pct_per_symbol <= 0 or max_pct_per_symbol > 1:
            raise ValueError("max_pct_per_symbol은 (0, 1] 범위여야 함")
        self._max_pct = max_pct_per_symbol

    def evaluate(self, ctx: RiskContext) -> GateResult:
        if ctx.side == "sell":
            return GateResult(gate_name=self.name, outcome="pass", reason=None)

        if ctx.equity_krw <= 0:
            return GateResult(
                gate_name=self.name, outcome="reject",
                reason="자본 비정상 (equity invalid)",
                detail={"equity_krw": str(ctx.equity_krw)},
            )

        existing = ctx.open_positions.get(ctx.symbol, {})
        existing_value = Decimal(str(existing.get("market_value_krw", "0")))
        projected_value = existing_value + ctx.estimated_cost_krw
        projected_pct = projected_value / ctx.equity_krw

        if projected_pct > self._max_pct:
            return GateResult(
                gate_name=self.name, outcome="reject",
                reason=f"종목 노출 한도 초과 (per-symbol exposure exceeded): "
                       f"projected_pct={projected_pct:.4f} > max={self._max_pct:.4f}",
                detail={
                    "symbol": ctx.symbol,
                    "existing_value_krw": str(existing_value),
                    "projected_value_krw": str(projected_value),
                    "projected_pct": f"{projected_pct:.6f}",
                    "max_pct": f"{self._max_pct:.6f}",
                },
            )
        return GateResult(gate_name=self.name, outcome="pass", reason=None)
