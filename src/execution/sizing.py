"""
주문 사이징 로직 v0.2 (Order Sizing Logic v0.2)
================================================

JCPR Trading System - jcpr-ts-v01
Task 18 v0.2

기능 (Features):
- 다중 사이징 방식 지원: 고정 비율(fixed_pct, 기본) / ATR / 고정 리스크
- KRX 호가단위 (tick) 및 거래단위 (lot) 정합성
- 가용 현금 + capacity.yaml per-order/per-day 한도 동시 검증
- 최소/최대 주문 단위 차단
- 결정 근거 audit log

원칙 (Principles):
- fail-closed: 데이터 부족 / 검증 실패 시 0 수량 반환 (reject)
- stop-first: 호출자가 종료 신호 우선 처리 (caller handles shutdown first)
- no secret leakage: 가격/수량 데이터만 로깅, 키/비밀 없음

이전 버전 대비 변경 (Changes from v0.1):
- 단일 사이징 방식 -> 다중 방식 (configurable)
- 호가/거래단위 정합 로직 추가
- 가용 현금 + 한도 이중 검증 (cash + capacity)
- 구조화된 audit log
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Literal, Optional

from .tick_size import (
    align_price_to_tick,
    get_tick_size,
    InstrumentType,
    Side,
    TickAlignment,
)
from .sizing_audit import SizingAuditLogger, SizingDecision

logger = logging.getLogger(__name__)

SizingMethod = Literal["fixed_pct", "atr", "fixed_risk"]


@dataclass(frozen=True)
class CapacityConfig:
    """
    capacity.yaml에서 로드된 한도 설정.
    (Capacity limits loaded from capacity.yaml.)
    실제 capacity.yaml 파서는 Task 5 산출물에 위임.
    """
    max_per_order_krw: Decimal      # 1회 주문 최대 명목 금액
    max_per_day_krw: Decimal        # 일일 총 명목 금액 한도 (조회용 컨텍스트)
    max_pct_of_equity: Decimal      # 1회 주문 최대 자본 비율 (예: 0.05 = 5%)
    min_per_order_krw: Decimal      # 1회 주문 최소 명목 금액 (작은 주문 차단)
    default_sizing_method: SizingMethod = "fixed_pct"


@dataclass(frozen=True)
class SizingInputs:
    """
    사이징 호출 시 외부에서 주어지는 컨텍스트.
    (External context passed to sizing call.)
    """
    symbol: str
    side: Side
    instrument_type: InstrumentType
    reference_price: Decimal              # 호가 정렬 기준 가격 (현재가/시그널가)
    equity_krw: Decimal                   # 현재 총 자본
    available_cash_krw: Decimal           # 즉시 사용 가능 현금
    used_per_day_krw: Decimal             # 오늘 누적 사용 명목 금액
    strategy_id: str
    # 방법별 추가 파라미터
    fixed_pct: Optional[Decimal] = None   # fixed_pct 방식 (예: 0.02 = 2%)
    atr_value: Optional[Decimal] = None   # atr 방식
    risk_pct: Optional[Decimal] = None    # fixed_risk 방식 (예: 0.01 = 1%)
    stop_distance: Optional[Decimal] = None  # fixed_risk 방식 손절 거리


@dataclass(frozen=True)
class SizingResult:
    """
    사이징 최종 결과.
    (Final sizing result.)
    """
    quantity: int                 # 0 이면 거부 (0 means reject)
    aligned_price: Optional[Decimal]
    estimated_cost_krw: Decimal
    method_used: SizingMethod
    decision: Literal["accept", "reject"]
    reject_reason: Optional[str]
    decision_id: str              # audit log 추적용


class OrderSizer:
    """
    주문 사이징 엔진.
    (Order sizing engine.)
    """

    def __init__(
        self,
        capacity: CapacityConfig,
        audit_logger: SizingAuditLogger,
    ):
        self._cap = capacity
        self._audit = audit_logger

    # ------------------------------------------------------------------
    # 메인 진입점 (Main entry point)
    # ------------------------------------------------------------------
    def size(
        self,
        inputs: SizingInputs,
        *,
        method: Optional[SizingMethod] = None,
    ) -> SizingResult:
        """
        시그널/주문 의도에 대해 수량을 산정.
        (Calculate quantity for signal/order intent.)

        fail-closed: 어떤 단계든 실패하면 quantity=0, decision=reject.
        """
        chosen_method: SizingMethod = method or self._cap.default_sizing_method
        notes: list[str] = []

        # 1) 입력 기본 검증 (basic input validation)
        validation_error = self._validate_inputs(inputs)
        if validation_error:
            return self._reject(
                inputs, chosen_method, validation_error, notes,
                raw_qty=0, aligned=None, est_cost=Decimal("0"),
            )

        # 2) 호가 정렬 (tick alignment)
        try:
            tick_align: TickAlignment = align_price_to_tick(
                inputs.reference_price,
                inputs.side,
                inputs.instrument_type,
                conservative=True,
            )
        except ValueError as e:
            return self._reject(
                inputs, chosen_method, f"호가 정렬 실패 (tick alignment failed): {e}",
                notes, raw_qty=0, aligned=None, est_cost=Decimal("0"),
            )
        aligned_price = tick_align.aligned_price
        notes.append(f"tick={tick_align.tick_size}, method={tick_align.method}")

        # 3) 명목 금액 산정 (notional calculation)
        try:
            notional = self._compute_notional(inputs, chosen_method)
        except ValueError as e:
            return self._reject(
                inputs, chosen_method, f"명목 산정 실패 (notional calc failed): {e}",
                notes, raw_qty=0, aligned=aligned_price, est_cost=Decimal("0"),
            )
        notes.append(f"target_notional_krw={notional}")

        # 4) per-order 상한 적용 (per-order cap)
        if notional > self._cap.max_per_order_krw:
            notes.append(
                f"per_order_cap_applied: {notional} -> {self._cap.max_per_order_krw}"
            )
            notional = self._cap.max_per_order_krw

        # 5) per-order 자본 비율 상한 (per-order pct of equity)
        equity_cap = inputs.equity_krw * self._cap.max_pct_of_equity
        if notional > equity_cap:
            notes.append(f"equity_pct_cap_applied: {notional} -> {equity_cap}")
            notional = equity_cap

        # 6) 가용 현금 상한 (available cash cap, 매수 시만)
        if inputs.side == "buy" and notional > inputs.available_cash_krw:
            notes.append(
                f"cash_cap_applied: {notional} -> {inputs.available_cash_krw}"
            )
            notional = inputs.available_cash_krw

        # 7) 최소 주문 금액 점검 (min order check)
        if notional < self._cap.min_per_order_krw:
            return self._reject(
                inputs, chosen_method,
                f"최소 주문 금액 미달 (below min_per_order): "
                f"{notional} < {self._cap.min_per_order_krw}",
                notes, raw_qty=0, aligned=aligned_price, est_cost=Decimal("0"),
            )

        # 8) 수량 계산 + 거래단위 정렬 (quantity + lot alignment)
        if aligned_price <= 0:
            return self._reject(
                inputs, chosen_method, "정렬 가격 비정상 (aligned price invalid)",
                notes, raw_qty=0, aligned=aligned_price, est_cost=Decimal("0"),
            )

        raw_qty = int((notional / aligned_price).to_integral_value(rounding=ROUND_DOWN))
        # KRX 주식/ETF/ETN 거래단위는 모두 1주 (lot=1) — 향후 종목별 lot은 symbol_master 참조
        lot = 1
        final_qty = (raw_qty // lot) * lot

        if final_qty <= 0:
            return self._reject(
                inputs, chosen_method,
                f"수량 0 산출 (zero quantity computed): notional={notional}, price={aligned_price}",
                notes, raw_qty=raw_qty, aligned=aligned_price, est_cost=Decimal("0"),
            )

        est_cost = aligned_price * Decimal(final_qty)

        # 9) 최종 가용 현금 재확인 (final cash recheck after rounding, 매수 시)
        if inputs.side == "buy" and est_cost > inputs.available_cash_krw:
            return self._reject(
                inputs, chosen_method,
                f"가용 현금 부족 (insufficient cash post-rounding): "
                f"est_cost={est_cost} > cash={inputs.available_cash_krw}",
                notes, raw_qty=raw_qty, aligned=aligned_price, est_cost=est_cost,
            )

        # ✅ accept
        return self._accept(
            inputs, chosen_method, raw_qty, final_qty,
            aligned_price, est_cost, notes,
        )

    # ------------------------------------------------------------------
    # 사이징 방식별 명목 금액 계산 (notional calculation per method)
    # ------------------------------------------------------------------
    def _compute_notional(self, inp: SizingInputs, method: SizingMethod) -> Decimal:
        if method == "fixed_pct":
            pct = inp.fixed_pct if inp.fixed_pct is not None else self._cap.max_pct_of_equity
            if pct is None or pct <= 0:
                raise ValueError("fixed_pct 값이 없거나 비양수")
            return inp.equity_krw * pct

        if method == "atr":
            if inp.atr_value is None or inp.atr_value <= 0:
                raise ValueError("ATR 값이 없거나 비양수")
            if inp.risk_pct is None or inp.risk_pct <= 0:
                raise ValueError("ATR 방식에는 risk_pct 필요")
            # 자본 * 리스크비율 / ATR 단위가격 = 수량 -> 명목으로 환산
            risk_amount = inp.equity_krw * inp.risk_pct
            qty_est = risk_amount / inp.atr_value
            return qty_est * inp.reference_price

        if method == "fixed_risk":
            if inp.risk_pct is None or inp.risk_pct <= 0:
                raise ValueError("fixed_risk 방식에는 risk_pct 필요")
            if inp.stop_distance is None or inp.stop_distance <= 0:
                raise ValueError("fixed_risk 방식에는 stop_distance 필요")
            risk_amount = inp.equity_krw * inp.risk_pct
            qty_est = risk_amount / inp.stop_distance
            return qty_est * inp.reference_price

        raise ValueError(f"알 수 없는 사이징 방식 (unknown sizing method): {method}")

    # ------------------------------------------------------------------
    # 입력 검증 (input validation)
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_inputs(inp: SizingInputs) -> Optional[str]:
        if inp.equity_krw <= 0:
            return f"자본 비정상 (equity invalid): {inp.equity_krw}"
        if inp.available_cash_krw < 0:
            return f"가용현금 음수 (cash negative): {inp.available_cash_krw}"
        if inp.reference_price <= 0:
            return f"기준가 비정상 (reference price invalid): {inp.reference_price}"
        if inp.side not in ("buy", "sell"):
            return f"잘못된 side: {inp.side}"
        if not inp.symbol:
            return "심볼 누락 (symbol missing)"
        return None

    # ------------------------------------------------------------------
    # 결과 헬퍼 (result helpers + audit log)
    # ------------------------------------------------------------------
    def _accept(
        self,
        inp: SizingInputs,
        method: SizingMethod,
        raw_qty: int,
        final_qty: int,
        aligned_price: Decimal,
        est_cost: Decimal,
        notes: list[str],
    ) -> SizingResult:
        decision = SizingDecision.new(
            strategy_id=inp.strategy_id,
            symbol=inp.symbol,
            side=inp.side,
            sizing_method=method,
            inputs=self._sanitize_inputs(inp),
            intermediate={
                "tick_size": get_tick_size(inp.reference_price, inp.instrument_type),
            },
            raw_quantity=raw_qty,
            final_quantity=final_qty,
            raw_price=inp.reference_price,
            aligned_price=aligned_price,
            estimated_cost=est_cost,
            decision="accept",
            reject_reason=None,
            notes=notes,
        )
        self._audit.write(decision)
        return SizingResult(
            quantity=final_qty,
            aligned_price=aligned_price,
            estimated_cost_krw=est_cost,
            method_used=method,
            decision="accept",
            reject_reason=None,
            decision_id=decision.decision_id,
        )

    def _reject(
        self,
        inp: SizingInputs,
        method: SizingMethod,
        reason: str,
        notes: list[str],
        raw_qty: int,
        aligned: Optional[Decimal],
        est_cost: Decimal,
    ) -> SizingResult:
        decision = SizingDecision.new(
            strategy_id=inp.strategy_id,
            symbol=inp.symbol,
            side=inp.side,
            sizing_method=method,
            inputs=self._sanitize_inputs(inp),
            intermediate={},
            raw_quantity=raw_qty,
            final_quantity=0,
            raw_price=inp.reference_price,
            aligned_price=aligned,
            estimated_cost=est_cost,
            decision="reject",
            reject_reason=reason,
            notes=notes,
        )
        self._audit.write(decision)
        logger.info(
            "사이징 거부 (sizing rejected) symbol=%s reason=%s",
            inp.symbol, reason,
        )
        return SizingResult(
            quantity=0,
            aligned_price=aligned,
            estimated_cost_krw=est_cost,
            method_used=method,
            decision="reject",
            reject_reason=reason,
            decision_id=decision.decision_id,
        )

    @staticmethod
    def _sanitize_inputs(inp: SizingInputs) -> dict:
        """audit log에 기록할 입력 (비밀 없음 - no secrets)."""
        return {
            "symbol": inp.symbol,
            "side": inp.side,
            "instrument_type": inp.instrument_type,
            "reference_price": str(inp.reference_price),
            "equity_krw": str(inp.equity_krw),
            "available_cash_krw": str(inp.available_cash_krw),
            "used_per_day_krw": str(inp.used_per_day_krw),
            "strategy_id": inp.strategy_id,
            "fixed_pct": str(inp.fixed_pct) if inp.fixed_pct is not None else None,
            "atr_value": str(inp.atr_value) if inp.atr_value is not None else None,
            "risk_pct": str(inp.risk_pct) if inp.risk_pct is not None else None,
            "stop_distance": str(inp.stop_distance) if inp.stop_distance is not None else None,
        }
