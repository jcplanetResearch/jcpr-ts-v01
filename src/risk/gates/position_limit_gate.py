"""포지션 한도 게이트 (Position Count Limit Gate)."""

from __future__ import annotations

from .base import GateResult, RiskContext, RiskGate


class PositionLimitGate(RiskGate):
    """
    동시 보유 종목 수가 한도 초과 시 신규 매수 거부.
    (Reject new buys when number of concurrent positions exceeds limit.)
    """

    name = "position_count_limit"

    def __init__(self, max_positions: int):
        if max_positions <= 0:
            raise ValueError("max_positions는 양의 정수")
        self._max = max_positions

    def evaluate(self, ctx: RiskContext) -> GateResult:
        if ctx.side == "sell":
            return GateResult(gate_name=self.name, outcome="pass", reason=None)

        current_count = len([
            s for s, p in ctx.open_positions.items()
            if int(p.get("quantity", 0)) > 0
        ])
        # 신규 종목인 경우만 카운트 증가
        if ctx.symbol not in ctx.open_positions or int(
            ctx.open_positions.get(ctx.symbol, {}).get("quantity", 0)
        ) == 0:
            projected = current_count + 1
        else:
            projected = current_count

        if projected > self._max:
            return GateResult(
                gate_name=self.name, outcome="reject",
                reason=f"동시 포지션 수 한도 초과 (position count limit exceeded): "
                       f"{projected} > {self._max}",
                detail={"current": current_count, "projected": projected, "max": self._max},
            )
        return GateResult(gate_name=self.name, outcome="pass", reason=None)
