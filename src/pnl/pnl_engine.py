"""
P&L 엔진 (P&L Engine)
=====================

JCPR Trading System - jcpr-ts-v01
Task 26 v0.1

Position Ledger (Task 25) + 시세 → 미실현 P&L + 종합 리포트.
(Position Ledger + market data → unrealized P&L + summary.)

가격 우선순위 (Price priority):
1. Quote mid-quote (Task 13 — fresh)
2. OHLCV 직전 종가 (Task 12)
3. None → stale

원칙 (Principles):
- fail-closed: stale 시 stale_symbols에 표시, total에서 제외
- v0.1: 단일 strategy 가정 (momentum_v04)
- 모든 datetime UTC tz-aware
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from ..data.ohlcv_schema import Timeframe
from ..data.ohlcv_store import OHLCVStore
from ..data.quote_store import QuoteStore
from .pnl_schema import PortfolioPnL, StrategyAttribution, SymbolPnL
from .position_ledger import PositionLedger

logger = logging.getLogger(__name__)


class PnLEngine:
    """
    P&L 계산 엔진.
    """

    def __init__(
        self,
        ledger: PositionLedger,
        ohlcv_store: OHLCVStore,
        quote_store: Optional[QuoteStore] = None,
        *,
        max_quote_age_sec: int = 30,
        ohlcv_timeframe: Timeframe = Timeframe.D1,
        default_strategy_id: str = "momentum_v04",
    ):
        self._ledger = ledger
        self._ohlcv = ohlcv_store
        self._quote = quote_store
        self._max_quote_age_sec = max_quote_age_sec
        self._ohlcv_timeframe = ohlcv_timeframe
        self._default_strategy_id = default_strategy_id

    # ------------------------------------------------------------------
    # 단일 종목 P&L
    # ------------------------------------------------------------------

    def compute_symbol_pnl(
        self,
        symbol: str,
        *,
        as_of_utc: datetime,
    ) -> SymbolPnL:
        """
        단일 종목 P&L.
        포지션이 없으면 빈 결과(quantity=0).
        """
        if as_of_utc.tzinfo is None:
            raise ValueError("as_of_utc tz-aware 필수")

        state = self._ledger.get(symbol)
        if state is None:
            # 포지션 자체가 없는 경우 — 0 반환
            return SymbolPnL(
                symbol=symbol,
                quantity=0,
                avg_cost_krw=Decimal("0"),
                realized_pnl_krw=Decimal("0"),
                current_price_krw=None,
                price_source="none",
                market_value_krw=Decimal("0"),
                unrealized_pnl_krw=None,
                total_pnl_krw=None,
                total_fees_krw=Decimal("0"),
                total_taxes_krw=Decimal("0"),
            )

        # 현재가 결정
        current_price, price_source = self._resolve_current_price(symbol, as_of_utc)

        # 미실현 + 시장가치
        if current_price is not None and state.quantity > 0:
            market_value = current_price * Decimal(state.quantity)
            unrealized = (current_price - state.avg_cost_krw) * Decimal(state.quantity)
            total_pnl = state.realized_pnl_krw + unrealized
        else:
            market_value = Decimal("0")
            unrealized = None
            # quantity=0이면 unrealized=0 처리 (보유 없음)
            if state.quantity == 0:
                unrealized = Decimal("0")
                total_pnl = state.realized_pnl_krw
            else:
                total_pnl = None  # 가격 없어 계산 불가

        return SymbolPnL(
            symbol=symbol,
            quantity=state.quantity,
            avg_cost_krw=state.avg_cost_krw,
            realized_pnl_krw=state.realized_pnl_krw,
            current_price_krw=current_price,
            price_source=price_source,
            market_value_krw=market_value,
            unrealized_pnl_krw=unrealized,
            total_pnl_krw=total_pnl,
            total_fees_krw=state.total_fees_krw,
            total_taxes_krw=state.total_taxes_krw,
        )

    # ------------------------------------------------------------------
    # 포트폴리오 종합
    # ------------------------------------------------------------------

    def compute_portfolio_pnl(
        self,
        *,
        starting_capital_krw: Decimal,
        cash_krw: Decimal,
        as_of_utc: datetime,
        include_closed_positions: bool = True,
    ) -> PortfolioPnL:
        """
        전체 포트폴리오 P&L.
        Final output #1-7 산출용.

        Args:
            starting_capital_krw: 세션/일일 시작 자본 (호출자 제공)
            cash_krw: 현재 가용 현금 (KIS account 또는 외부)
            as_of_utc: 평가 시각
            include_closed_positions: 청산된 종목(qty=0)도 포함 (realized P&L 추적용)
        """
        if as_of_utc.tzinfo is None:
            raise ValueError("as_of_utc tz-aware 필수")
        if starting_capital_krw < 0:
            raise ValueError(f"starting_capital_krw 음수 불가: {starting_capital_krw}")
        if cash_krw < 0:
            raise ValueError(f"cash_krw 음수 불가: {cash_krw}")

        # 모든 포지션 (활성 + 청산)
        all_states = self._ledger.get_all(only_active=False)

        positions: dict[str, SymbolPnL] = {}
        stale: list[str] = []

        total_realized = Decimal("0")
        total_unrealized = Decimal("0")
        total_market_value = Decimal("0")
        total_fees = Decimal("0")
        total_taxes = Decimal("0")

        by_symbol_realized: dict[str, Decimal] = {}
        by_symbol_unrealized: dict[str, Optional[Decimal]] = {}

        for symbol, state in all_states.items():
            # 청산된 포지션은 옵션
            if state.quantity == 0 and not include_closed_positions:
                # 그래도 누적 realized는 합산
                total_realized += state.realized_pnl_krw
                total_fees += state.total_fees_krw
                total_taxes += state.total_taxes_krw
                by_symbol_realized[symbol] = state.realized_pnl_krw
                by_symbol_unrealized[symbol] = Decimal("0")
                continue

            sym_pnl = self.compute_symbol_pnl(symbol, as_of_utc=as_of_utc)
            positions[symbol] = sym_pnl

            total_realized += sym_pnl.realized_pnl_krw
            total_fees += sym_pnl.total_fees_krw
            total_taxes += sym_pnl.total_taxes_krw
            by_symbol_realized[symbol] = sym_pnl.realized_pnl_krw
            by_symbol_unrealized[symbol] = sym_pnl.unrealized_pnl_krw

            # 미실현/시장가치 (None이면 stale 표기 + 합산 제외)
            if sym_pnl.unrealized_pnl_krw is None and sym_pnl.is_active():
                stale.append(symbol)
            else:
                if sym_pnl.unrealized_pnl_krw is not None:
                    total_unrealized += sym_pnl.unrealized_pnl_krw
                total_market_value += sym_pnl.market_value_krw

        # ending capital = cash + market value
        ending_capital = cash_krw + total_market_value

        # Strategy attribution (v0.1: 단일)
        active_symbols = [s for s, p in positions.items() if p.is_active()]
        all_symbols_in_strategy = list(all_states.keys())
        total_fills = sum(s.fills_processed for s in all_states.values())
        strategy_attr = [
            StrategyAttribution(
                strategy_id=self._default_strategy_id,
                realized_pnl_krw=total_realized,
                unrealized_pnl_krw=total_unrealized,
                fills_count=total_fills,
                symbols=sorted(all_symbols_in_strategy),
            )
        ]

        return PortfolioPnL(
            captured_at_utc=as_of_utc,
            starting_capital_krw=starting_capital_krw,
            cash_krw=cash_krw,
            positions=positions,
            total_realized_pnl_krw=total_realized,
            total_unrealized_pnl_krw=total_unrealized,
            total_market_value_krw=total_market_value,
            total_fees_krw=total_fees,
            total_taxes_krw=total_taxes,
            ending_capital_krw=ending_capital,
            stale_symbols=sorted(stale),
            by_symbol_realized_krw=by_symbol_realized,
            by_symbol_unrealized_krw=by_symbol_unrealized,
            by_strategy=strategy_attr,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_current_price(
        self, symbol: str, as_of_utc: datetime,
    ) -> tuple[Optional[Decimal], str]:
        """
        현재가 결정.
        1) Quote mid (fresh)
        2) OHLCV 최신 종가
        3) None
        """
        # 1) Quote
        if self._quote is not None:
            try:
                snap = self._quote.latest_fresh(
                    symbol, as_of_utc, self._max_quote_age_sec,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("Quote 조회 실패: %s — %s", symbol, e)
                snap = None
            if snap is not None:
                return snap.mid_quote(), "quote"

        # 2) OHLCV
        try:
            latest_bar = self._ohlcv.latest_bar(symbol, self._ohlcv_timeframe)
        except Exception as e:  # noqa: BLE001
            logger.warning("OHLCV 조회 실패: %s — %s", symbol, e)
            latest_bar = None
        if latest_bar is not None:
            return latest_bar.close, "ohlcv"

        # 3) None
        return None, "none"
