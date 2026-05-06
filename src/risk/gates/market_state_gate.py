"""시장 상태 게이트 (Market State Gate) — Task 11 캘린더 연동."""

from __future__ import annotations

from .base import GateResult, RiskContext, RiskGate


class MarketStateGate(RiskGate):
    """
    KRX 정규장 개장 여부 확인.
    (Verify KRX regular session is open.)

    실제 캘린더 판정은 Task 11의 calendar 모듈이 ctx.market_is_open으로 전달.
    """

    name = "market_state"

    def evaluate(self, ctx: RiskContext) -> GateResult:
        if not ctx.market_is_open:
            return GateResult(
                gate_name=self.name,
                outcome="reject",
                reason="시장 미개장 (market not open)",
                detail={"now_utc": ctx.market_now_utc.isoformat()},
            )
        return GateResult(gate_name=self.name, outcome="pass", reason=None)
