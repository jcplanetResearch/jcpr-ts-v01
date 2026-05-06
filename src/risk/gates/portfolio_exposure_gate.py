"""
포트폴리오 전체 노출 게이트 (Portfolio Total Exposure Gate)
=============================================================

JCPR Trading System - jcpr-ts-v01
Task 47 v0.1

전체 보유 평가액 + 후보 매수 비용이 자본의 N% 초과 시 거부.
(Reject when total market value + candidate buy cost > N% of equity.)

기존 Task 19 ExposureGate와의 차이:
- ExposureGate: 종목 1개의 노출 한도 (per-symbol)
- PortfolioExposureGate: 모든 종목 합산 노출 한도 (aggregate)

원칙:
- BUY만 검사 (SELL은 노출 감소 — pass)
- equity_krw <= 0 → reject (fail-closed)
- 미체결 주문은 미반영 (현재 보유 + 후보만)
"""

from __future__ import annotations

from decimal import Decimal

from .base import GateResult, RiskContext, RiskGate


class PortfolioExposureGate(RiskGate):
    """전체 포트폴리오 노출이 자본의 N% 초과 시 BUY 거부."""

    name = "portfolio_total_exposure"

    def __init__(self, max_total_exposure_pct: Decimal):
        if max_total_exposure_pct <= 0 or max_total_exposure_pct > 1:
            raise ValueError(
                f"max_total_exposure_pct는 (0, 1] 범위: {max_total_exposure_pct}"
            )
        self._max_pct = max_total_exposure_pct

    def evaluate(self, ctx: RiskContext) -> GateResult:
        # SELL은 노출 감소 → 항상 pass
        if ctx.side == "sell":
            return GateResult(gate_name=self.name, outcome="pass", reason=None)

        if ctx.equity_krw <= 0:
            return GateResult(
                gate_name=self.name, outcome="reject",
                reason="자본 비정상 (equity invalid)",
                detail={"equity_krw": str(ctx.equity_krw)},
            )

        # 현재 모든 종목의 평가액 합산
        current_total = Decimal("0")
        for sym, pos in ctx.open_positions.items():
            mv = pos.get("market_value_krw", "0")
            try:
                current_total += Decimal(str(mv))
            except Exception:  # noqa: BLE001
                # 비정상 데이터 — 보수적으로 무시 (나머지 합산 진행)
                continue

        projected_total = current_total + ctx.estimated_cost_krw
        projected_pct = projected_total / ctx.equity_krw

        if projected_pct > self._max_pct:
            return GateResult(
                gate_name=self.name, outcome="reject",
                reason=(
                    f"포트폴리오 전체 노출 한도 초과 "
                    f"(portfolio total exposure exceeded): "
                    f"projected_pct={projected_pct:.4f} > max={self._max_pct:.4f}"
                ),
                detail={
                    "current_total_krw": str(current_total),
                    "candidate_cost_krw": str(ctx.estimated_cost_krw),
                    "projected_total_krw": str(projected_total),
                    "equity_krw": str(ctx.equity_krw),
                    "projected_pct": f"{projected_pct:.6f}",
                    "max_pct": f"{self._max_pct:.6f}",
                    "position_count": len(ctx.open_positions),
                },
            )

        return GateResult(
            gate_name=self.name, outcome="pass", reason=None,
            detail={
                "projected_pct": f"{projected_pct:.6f}",
                "max_pct": f"{self._max_pct:.6f}",
            },
        )
