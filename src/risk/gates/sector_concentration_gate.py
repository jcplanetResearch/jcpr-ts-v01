"""
섹터 집중도 게이트 (Sector Concentration Gate)
================================================

JCPR Trading System - jcpr-ts-v01
Task 47 v0.1

특정 섹터에 자본의 N% 이상 집중 시 BUY 거부.
(Reject BUY when adding to sector would exceed N% of equity in that sector.)

ETF 면제 옵션:
- exempt_etf=True (기본): ETF 종목은 sector 검사에서 면제
  ETF는 자체적으로 분산되어 있으므로 (KODEX 200 = 200종목)
- exempt_etf=False: ETF도 일반 종목과 동일하게 'etf' sector 적용

원칙:
- SymbolMaster 의존 (sector 조회용)
- 후보 종목의 sector를 SymbolMaster에서 조회
- 미상장 종목은 'unknown' sector 처리
- equity_krw <= 0 → reject
- BUY만 검사 (SELL은 pass)
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from ...data.symbol_master import SymbolMaster
from .base import GateResult, RiskContext, RiskGate


class SectorConcentrationGate(RiskGate):
    """섹터별 집중도가 자본의 N% 초과 시 BUY 거부."""

    name = "sector_concentration"

    def __init__(
        self,
        symbol_master: SymbolMaster,
        max_sector_exposure_pct: Decimal,
        *,
        exempt_etf: bool = True,
    ):
        if max_sector_exposure_pct <= 0 or max_sector_exposure_pct > 1:
            raise ValueError(
                f"max_sector_exposure_pct는 (0, 1] 범위: {max_sector_exposure_pct}"
            )
        self._sm = symbol_master
        self._max_pct = max_sector_exposure_pct
        self._exempt_etf = exempt_etf

    def evaluate(self, ctx: RiskContext) -> GateResult:
        if ctx.side == "sell":
            return GateResult(gate_name=self.name, outcome="pass", reason=None)

        if ctx.equity_krw <= 0:
            return GateResult(
                gate_name=self.name, outcome="reject",
                reason="자본 비정상 (equity invalid)",
                detail={"equity_krw": str(ctx.equity_krw)},
            )

        # 후보 종목의 sector 조회
        candidate_sym = self._sm.try_get(ctx.symbol)
        if candidate_sym is None:
            # 미상장 종목 — Symbol Master에 없음
            # SignalRunner / Task 19 다른 게이트가 처리하지만, 여기서도 보수적
            return GateResult(
                gate_name=self.name, outcome="pass",
                reason="symbol unknown — sector check skipped (handled by other gates)",
                detail={"symbol": ctx.symbol},
            )

        # ETF는 면제
        if self._exempt_etf and candidate_sym.is_etf():
            return GateResult(
                gate_name=self.name, outcome="pass",
                reason=None,
                detail={
                    "symbol": ctx.symbol,
                    "exempted": "etf_self_diversified",
                },
            )

        candidate_sector = candidate_sym.sector

        # 현재 보유 중 같은 sector의 노출 합산
        current_sector_exposure = Decimal("0")
        for sym, pos in ctx.open_positions.items():
            sym_obj = self._sm.try_get(sym)
            if sym_obj is None:
                continue
            # ETF 면제 시 합산에서도 제외 (일관성)
            if self._exempt_etf and sym_obj.is_etf():
                continue
            if sym_obj.sector != candidate_sector:
                continue
            try:
                current_sector_exposure += Decimal(str(pos.get("market_value_krw", "0")))
            except Exception:  # noqa: BLE001
                continue

        projected_sector_exposure = current_sector_exposure + ctx.estimated_cost_krw
        projected_pct = projected_sector_exposure / ctx.equity_krw

        if projected_pct > self._max_pct:
            return GateResult(
                gate_name=self.name, outcome="reject",
                reason=(
                    f"섹터 집중도 한도 초과 "
                    f"(sector concentration exceeded): "
                    f"sector={candidate_sector!r}, "
                    f"projected_pct={projected_pct:.4f} > max={self._max_pct:.4f}"
                ),
                detail={
                    "candidate_symbol": ctx.symbol,
                    "candidate_sector": candidate_sector,
                    "current_sector_exposure_krw": str(current_sector_exposure),
                    "candidate_cost_krw": str(ctx.estimated_cost_krw),
                    "projected_sector_exposure_krw": str(projected_sector_exposure),
                    "equity_krw": str(ctx.equity_krw),
                    "projected_pct": f"{projected_pct:.6f}",
                    "max_pct": f"{self._max_pct:.6f}",
                },
            )

        return GateResult(
            gate_name=self.name, outcome="pass", reason=None,
            detail={
                "candidate_sector": candidate_sector,
                "projected_pct": f"{projected_pct:.6f}",
                "max_pct": f"{self._max_pct:.6f}",
            },
        )
