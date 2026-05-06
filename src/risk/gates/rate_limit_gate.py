"""
주문 빈도 제한 게이트 (Order Rate Limit Gate)
=============================================

≥5초 최소 간격 (전체) + 종목별 N초 쿨다운.
(≥5 sec minimum interval globally + per-symbol N sec cooldown.)
"""

from __future__ import annotations

from datetime import timedelta

from .base import GateResult, RiskContext, RiskGate


class OrderRateLimitGate(RiskGate):
    """
    Sequential MVP 정책: 시스템 전체 직전 주문 후 최소 간격, 동일 종목은 더 긴 쿨다운.
    (Sequential MVP policy.)
    """

    name = "order_rate_limit"

    def __init__(
        self,
        global_min_interval_sec: int = 5,
        per_symbol_cooldown_sec: int = 30,
    ):
        if global_min_interval_sec < 5:
            raise ValueError("global_min_interval_sec >= 5 (요구사항)")
        self._global = timedelta(seconds=global_min_interval_sec)
        self._per_symbol = timedelta(seconds=per_symbol_cooldown_sec)

    def evaluate(self, ctx: RiskContext) -> GateResult:
        now = ctx.market_now_utc

        if ctx.last_order_at_utc is not None:
            elapsed = now - ctx.last_order_at_utc
            if elapsed < self._global:
                remaining = (self._global - elapsed).total_seconds()
                return GateResult(
                    gate_name=self.name, outcome="reject",
                    reason=f"전역 주문 간격 미달 (global interval too short): "
                           f"need {remaining:.2f}s more",
                    detail={
                        "elapsed_sec": elapsed.total_seconds(),
                        "min_interval_sec": self._global.total_seconds(),
                    },
                )

        if ctx.last_order_for_symbol_utc is not None:
            elapsed_sym = now - ctx.last_order_for_symbol_utc
            if elapsed_sym < self._per_symbol:
                remaining = (self._per_symbol - elapsed_sym).total_seconds()
                return GateResult(
                    gate_name=self.name, outcome="reject",
                    reason=f"종목 쿨다운 미경과 (per-symbol cooldown active): "
                           f"need {remaining:.2f}s more",
                    detail={
                        "symbol": ctx.symbol,
                        "elapsed_sec": elapsed_sym.total_seconds(),
                        "cooldown_sec": self._per_symbol.total_seconds(),
                    },
                )

        return GateResult(gate_name=self.name, outcome="pass", reason=None)
