"""
P&L 데이터 모델 (P&L Data Models)
===================================

JCPR Trading System - jcpr-ts-v01
Task 26 v0.1

종목별 P&L + 포트폴리오 종합 P&L.
(Per-symbol P&L + portfolio aggregate P&L.)

원칙 (Principles):
- frozen=True (immutable)
- UTC tz-aware datetime
- Decimal 정밀도 보존
- Stale 가격은 None으로 명시 (호출자가 인지)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Optional


@dataclass(frozen=True)
class SymbolPnL:
    """
    단일 종목 P&L.
    (Per-symbol P&L.)
    """
    symbol: str
    quantity: int                                # 현재 보유 수량
    avg_cost_krw: Decimal                        # 평균 매입가 (Task 25)
    realized_pnl_krw: Decimal                    # 누적 실현 손익
    current_price_krw: Optional[Decimal]         # 현재가 (None=stale)
    price_source: str                            # "quote" / "ohlcv" / "none"
    market_value_krw: Decimal                    # qty * current_price (없으면 0)
    unrealized_pnl_krw: Optional[Decimal]        # None=계산 불가
    total_pnl_krw: Optional[Decimal]             # realized + unrealized
    total_fees_krw: Decimal                      # 누적 수수료
    total_taxes_krw: Decimal                     # 누적 거래세

    def has_current_price(self) -> bool:
        return self.current_price_krw is not None

    def is_active(self) -> bool:
        return self.quantity > 0


@dataclass(frozen=True)
class StrategyAttribution:
    """전략별 P&L 귀속 (final output #6)."""
    strategy_id: str
    realized_pnl_krw: Decimal
    unrealized_pnl_krw: Optional[Decimal]
    fills_count: int
    symbols: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PortfolioPnL:
    """
    포트폴리오 전체 P&L 스냅샷.
    Final output 산출용 — items #1-7.
    """
    captured_at_utc: datetime

    # final output #1
    starting_capital_krw: Decimal

    # cash + positions
    cash_krw: Decimal
    positions: dict[str, SymbolPnL] = field(default_factory=dict)

    # final output #3 (realized), #4 (unrealized), #5 (fees+taxes)
    total_realized_pnl_krw: Decimal = Decimal("0")
    total_unrealized_pnl_krw: Decimal = Decimal("0")     # None은 0으로 합산
    total_market_value_krw: Decimal = Decimal("0")
    total_fees_krw: Decimal = Decimal("0")
    total_taxes_krw: Decimal = Decimal("0")

    # final output #2
    ending_capital_krw: Decimal = Decimal("0")

    # 신선도
    stale_symbols: list[str] = field(default_factory=list)

    # final output #6, #7
    by_symbol_realized_krw: dict[str, Decimal] = field(default_factory=dict)
    by_symbol_unrealized_krw: dict[str, Optional[Decimal]] = field(default_factory=dict)
    by_strategy: list[StrategyAttribution] = field(default_factory=list)

    def total_pnl_krw(self) -> Decimal:
        """총 P&L = 실현 + 미실현 - 수수료 - 거래세."""
        # realized/unrealized는 이미 fee/tax 차감 후 (Task 25 기준)
        # 따라서 단순 합
        return self.total_realized_pnl_krw + self.total_unrealized_pnl_krw

    def return_pct(self) -> Optional[Decimal]:
        """starting_capital 대비 수익률 (Decimal, 소수)."""
        if self.starting_capital_krw <= 0:
            return None
        return self.total_pnl_krw() / self.starting_capital_krw

    def to_summary_dict(self) -> dict:
        """
        Final output 형식 요약 (#1-7 + 보조 항목).
        호출자(예: 일일 리포트)가 그대로 출력 가능.
        """
        return {
            "captured_at_utc": self.captured_at_utc.isoformat(),
            # final output #1
            "starting_capital_krw": str(self.starting_capital_krw),
            # final output #2
            "ending_capital_krw": str(self.ending_capital_krw),
            # final output #3
            "realized_pnl_krw": str(self.total_realized_pnl_krw),
            # final output #4
            "unrealized_pnl_krw": str(self.total_unrealized_pnl_krw),
            # final output #5
            "fees_krw": str(self.total_fees_krw),
            "taxes_krw": str(self.total_taxes_krw),
            # final output #6
            "strategy_attribution": [
                {
                    "strategy_id": s.strategy_id,
                    "realized_pnl_krw": str(s.realized_pnl_krw),
                    "unrealized_pnl_krw": (
                        str(s.unrealized_pnl_krw) if s.unrealized_pnl_krw is not None else None
                    ),
                    "fills_count": s.fills_count,
                    "symbols": s.symbols,
                }
                for s in self.by_strategy
            ],
            # final output #7
            "symbol_attribution": {
                sym: {
                    "realized_krw": str(self.by_symbol_realized_krw.get(sym, Decimal("0"))),
                    "unrealized_krw": (
                        str(self.by_symbol_unrealized_krw.get(sym))
                        if self.by_symbol_unrealized_krw.get(sym) is not None else None
                    ),
                    "quantity": self.positions[sym].quantity if sym in self.positions else 0,
                    "current_price_krw": (
                        str(self.positions[sym].current_price_krw)
                        if sym in self.positions and self.positions[sym].current_price_krw is not None
                        else None
                    ),
                }
                for sym in set(self.by_symbol_realized_krw) | set(self.positions)
            },
            # 부가 정보
            "cash_krw": str(self.cash_krw),
            "total_market_value_krw": str(self.total_market_value_krw),
            "total_pnl_krw": str(self.total_pnl_krw()),
            "return_pct": (
                str(self.return_pct()) if self.return_pct() is not None else None
            ),
            "stale_symbols": self.stale_symbols,
        }
