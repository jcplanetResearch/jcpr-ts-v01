"""
포지션 상태 + 갱신 로직 (Position State + Update Logic)
========================================================

JCPR Trading System - jcpr-ts-v01
Task 25 v0.1

평균단가 방식 (Average Cost Method) — 한국 주식 표준.
(Average Cost — KR stock standard.)

핵심 알고리즘 (Core Algorithm):
    매수 (BUY):
        new_qty = old_qty + fill.qty
        new_cost = old_qty * old_avg + fill.qty * fill.price + fill.fee
        new_avg = new_cost / new_qty
        realized_pnl 변화 없음 (매수는 미실현)

    매도 (SELL):
        gross_proceeds = fill.qty * fill.price
        cost_basis = fill.qty * old_avg
        realized_delta = gross_proceeds - cost_basis - fill.fee - fill.tax
        new_qty = old_qty - fill.qty
        new_avg = old_avg (변화 없음, qty=0이면 0으로 reset)
        realized_pnl += realized_delta

원칙 (Principles):
- v0.1: 공매도 금지 (매도 수량 > 보유 → ValueError)
- 매수 수수료는 cost에 포함 (보수적, 정확한 손익)
- 모든 datetime UTC tz-aware
- Decimal 정밀도 보존
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from typing import Optional

from ..execution.fills import Fill, FillSide


@dataclass(frozen=True)
class PositionState:
    """
    종목 1건의 보유 상태 스냅샷.
    (Snapshot of one symbol's position.)
    """
    symbol: str
    quantity: int                         # 보유 수량 (v0.1: 항상 ≥ 0)
    avg_cost_krw: Decimal                 # 가중 평균 매입가 (수수료 포함)
    realized_pnl_krw: Decimal             # 누적 실현 손익
    total_fees_krw: Decimal               # 누적 수수료
    total_taxes_krw: Decimal              # 누적 거래세
    last_updated_utc: Optional[datetime] = None
    fills_processed: int = 0              # 누적 처리된 fill 수

    def __post_init__(self) -> None:
        if self.quantity < 0:
            raise ValueError(
                f"v0.1: 공매도 금지, quantity 음수 불가: {self.quantity}"
            )
        if self.avg_cost_krw < 0:
            raise ValueError(f"avg_cost_krw 음수 불가: {self.avg_cost_krw}")
        if self.total_fees_krw < 0:
            raise ValueError(f"total_fees_krw 음수 불가: {self.total_fees_krw}")
        if self.total_taxes_krw < 0:
            raise ValueError(f"total_taxes_krw 음수 불가: {self.total_taxes_krw}")
        if self.last_updated_utc is not None and self.last_updated_utc.tzinfo is None:
            raise ValueError("last_updated_utc tz-aware 필수")
        if self.fills_processed < 0:
            raise ValueError(f"fills_processed 음수 불가: {self.fills_processed}")

    @classmethod
    def empty(cls, symbol: str) -> "PositionState":
        """빈 포지션 (신규)."""
        return cls(
            symbol=symbol,
            quantity=0,
            avg_cost_krw=Decimal("0"),
            realized_pnl_krw=Decimal("0"),
            total_fees_krw=Decimal("0"),
            total_taxes_krw=Decimal("0"),
            last_updated_utc=None,
            fills_processed=0,
        )

    def is_active(self) -> bool:
        """현재 보유 중인지 (quantity > 0)."""
        return self.quantity > 0

    def cost_basis_krw(self) -> Decimal:
        """현재 보유 분의 총 매입원가 (qty * avg)."""
        return self.avg_cost_krw * Decimal(self.quantity)


@dataclass(frozen=True)
class FillApplicationResult:
    """체결 1건 적용 결과 (디버그/감사용)."""
    new_state: PositionState
    realized_pnl_delta_krw: Decimal       # 이 fill에서 발생한 실현 P&L
    fill_id: str


# ─────────────────────────────────────────────────
# 핵심 갱신 로직 (Core Update Logic)
# ─────────────────────────────────────────────────

class PositionLogicError(ValueError):
    """포지션 갱신 로직 오류 (예: 매도 > 보유)."""


def apply_fill_to_state(
    state: PositionState,
    fill: Fill,
) -> FillApplicationResult:
    """
    PositionState + Fill → 새 PositionState (불변).
    (Immutable update.)

    Raises:
        PositionLogicError: 매도 수량 > 보유, 종목 불일치 등
    """
    # 종목 일치 검증
    if state.symbol != fill.symbol:
        raise PositionLogicError(
            f"종목 불일치 (symbol mismatch): state={state.symbol}, fill={fill.symbol}"
        )

    if fill.side == FillSide.BUY:
        return _apply_buy(state, fill)
    else:  # SELL
        return _apply_sell(state, fill)


def _apply_buy(state: PositionState, fill: Fill) -> FillApplicationResult:
    """
    매수 적용:
        new_qty = old_qty + fill.qty
        new_cost = old_qty * old_avg + fill.qty * fill.price + fill.fee
        new_avg = new_cost / new_qty
    """
    old_qty = state.quantity
    new_qty = old_qty + fill.quantity

    # 평균단가 갱신 (수수료 포함)
    old_total_cost = state.avg_cost_krw * Decimal(old_qty)
    fill_cost = fill.price * Decimal(fill.quantity) + fill.fee_krw
    new_total_cost = old_total_cost + fill_cost
    new_avg = new_total_cost / Decimal(new_qty)

    new_state = PositionState(
        symbol=state.symbol,
        quantity=new_qty,
        avg_cost_krw=new_avg,
        realized_pnl_krw=state.realized_pnl_krw,  # 매수는 실현 영향 없음
        total_fees_krw=state.total_fees_krw + fill.fee_krw,
        total_taxes_krw=state.total_taxes_krw,    # 매수에 거래세 없음
        last_updated_utc=fill.filled_at_utc,
        fills_processed=state.fills_processed + 1,
    )
    return FillApplicationResult(
        new_state=new_state,
        realized_pnl_delta_krw=Decimal("0"),
        fill_id=fill.fill_id,
    )


def _apply_sell(state: PositionState, fill: Fill) -> FillApplicationResult:
    """
    매도 적용:
        v0.1: 보유량 미만 매도만 허용 (공매도 금지)

        gross = fill.qty * fill.price
        cost_basis = fill.qty * old_avg
        realized_delta = gross - cost_basis - fill.fee - fill.tax

        new_qty = old_qty - fill.qty
        new_avg = old_avg (qty>0이면 유지, qty=0이면 0)
    """
    if fill.quantity > state.quantity:
        raise PositionLogicError(
            f"매도 수량 > 보유 수량 (공매도 금지 v0.1): "
            f"sell_qty={fill.quantity}, holding={state.quantity}, symbol={fill.symbol}"
        )

    gross = fill.price * Decimal(fill.quantity)
    cost_basis = state.avg_cost_krw * Decimal(fill.quantity)
    realized_delta = gross - cost_basis - fill.fee_krw - fill.tax_krw

    new_qty = state.quantity - fill.quantity
    new_avg = state.avg_cost_krw if new_qty > 0 else Decimal("0")

    new_state = PositionState(
        symbol=state.symbol,
        quantity=new_qty,
        avg_cost_krw=new_avg,
        realized_pnl_krw=state.realized_pnl_krw + realized_delta,
        total_fees_krw=state.total_fees_krw + fill.fee_krw,
        total_taxes_krw=state.total_taxes_krw + fill.tax_krw,
        last_updated_utc=fill.filled_at_utc,
        fills_processed=state.fills_processed + 1,
    )
    return FillApplicationResult(
        new_state=new_state,
        realized_pnl_delta_krw=realized_delta,
        fill_id=fill.fill_id,
    )
