"""일일 손실 한도 게이트 (Daily Loss Limit Gate)."""

from __future__ import annotations

from decimal import Decimal

from .base import GateResult, RiskContext, RiskGate


class DailyLossLimitGate(RiskGate):
    """
    오늘 누적 실현 손실이 한도 도달 시 신규 매수 거부.
    (Reject new buys when cumulative daily realized loss hits limit.)

    매도(청산)는 기본적으로 허용 — 위험 축소 거래는 차단하지 않음.
    (Sells/closing trades allowed by default — do not block risk reduction.)
    """

    name = "daily_loss_limit"

    def __init__(self, max_daily_loss_krw: Decimal, *, block_sells: bool = False):
        if max_daily_loss_krw <= 0:
            raise ValueError("max_daily_loss_krw는 양수여야 함 (must be positive)")
        self._limit = max_daily_loss_krw
        self._block_sells = block_sells

    def evaluate(self, ctx: RiskContext) -> GateResult:
        # daily_realized_pnl_krw 가 음수일 때 손실 — 절대값 비교
        loss = -ctx.daily_realized_pnl_krw if ctx.daily_realized_pnl_krw < 0 else Decimal("0")
        if loss >= self._limit:
            if ctx.side == "sell" and not self._block_sells:
                return GateResult(
                    gate_name=self.name, outcome="pass",
                    reason=None,
                    detail={"loss_krw": str(loss), "limit": str(self._limit), "note": "sell allowed for risk reduction"},
                )
            return GateResult(
                gate_name=self.name, outcome="reject",
                reason=f"일일 손실 한도 도달 (daily loss limit reached): "
                       f"loss={loss} >= limit={self._limit}",
                detail={"loss_krw": str(loss), "limit_krw": str(self._limit)},
            )
        return GateResult(gate_name=self.name, outcome="pass", reason=None)
