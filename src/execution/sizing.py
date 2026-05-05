"""src/execution/sizing.py — Task 18: 사이징 (Sizing).

DRAFT 상태 OrderIntent → SIZED 상태 OrderIntent 변환.
시그널 + 가격 + 잔고 + capacity 설정을 입력으로 받아
주문 수량(quantity) 을 결정한다.

정책 (Policies)
--------------
- FIXED_NOTIONAL : 주문당 고정 명목금액
- FIXED_QUANTITY : 주문당 고정 수량
- PCT_CAPITAL    : 가용 잔고의 %

알고리즘 (Algorithm)
-------------------
1. 입력 검증 (price > 0, capacity 설정 존재, 잔고 양수)
2. 정책별 원시 수량(raw quantity) 계산
3. capacity.per_order 한도 적용 (clamp)
4. capacity.min_cash 보존 검증 (필요 시 재 clamp)
5. quantity > 0 검증
6. KRX lot size 보정
7. quantity > 0 재검증
8. SIZED 상태로 OrderIntent 갱신, sizing_metadata 기록

Fail-closed
----------
입력 누락·잘못된 타입·예외 → 모두 REJECTED 반환 (raise 하지 않음).
거부 사유는 Task 19 의 RejectionReason 재사용.

이중 방어 (Defense in Depth)
---------------------------
Sizing 의 clamp 와 risk_gate 의 검사는 동일 한도를 양쪽에서 검사.
Sizing 에서 clamp 했더라도 risk_gate 에서 재검사 → 한도 우회 불가.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from enum import Enum
from typing import Any, Optional

from src.brokers.types import OrderType, Side
from src.execution._fees import estimate_fee_krw
from src.execution.order_intent import IntentState, OrderIntent
from src.risk._decision import RejectionReason

logger = logging.getLogger(__name__)


# ============================================================
# 1. 사이징 정책 (Sizing Policy Enum)
# ============================================================

class SizingPolicy(str, Enum):
    """3가지 사이징 정책."""
    FIXED_NOTIONAL = "FIXED_NOTIONAL"
    FIXED_QUANTITY = "FIXED_QUANTITY"
    PCT_CAPITAL = "PCT_CAPITAL"


# ============================================================
# 2. 사이징 설정 (Sizing Config)
# ============================================================

@dataclass(frozen=True)
class SizingConfig:
    """사이징 정책별 매개변수.

    각 정책에 대해 정확히 하나의 매개변수를 사용한다.
    """
    policy: SizingPolicy
    # FIXED_NOTIONAL 용
    fixed_notional_krw: Optional[Decimal] = None
    # FIXED_QUANTITY 용
    fixed_quantity: Optional[int] = None
    # PCT_CAPITAL 용 (0.0 ~ 1.0)
    pct_capital: Optional[Decimal] = None

    def __post_init__(self) -> None:
        if self.policy == SizingPolicy.FIXED_NOTIONAL:
            if self.fixed_notional_krw is None or self.fixed_notional_krw <= 0:
                raise ValueError(
                    "FIXED_NOTIONAL requires fixed_notional_krw > 0"
                )
        elif self.policy == SizingPolicy.FIXED_QUANTITY:
            if self.fixed_quantity is None or self.fixed_quantity <= 0:
                raise ValueError(
                    "FIXED_QUANTITY requires fixed_quantity > 0"
                )
        elif self.policy == SizingPolicy.PCT_CAPITAL:
            if self.pct_capital is None:
                raise ValueError("PCT_CAPITAL requires pct_capital")
            if not (Decimal("0") < self.pct_capital <= Decimal("1")):
                raise ValueError(
                    f"pct_capital must be in (0, 1], got {self.pct_capital}"
                )


# ============================================================
# 3. Capacity 한도 (Capacity Limits)
# ============================================================

@dataclass(frozen=True)
class CapacityLimits:
    """Task 5 capacity.yaml 의 사이징 관련 부분만 추출.

    실제 운영에서는 yaml 로딩 헬퍼가 본 객체를 생성한다.
    """
    per_order_max_krw: Decimal       # 주문당 최대 명목금액
    min_cash_reserve_krw: Decimal    # 최소 현금 잔고 보존
    default_lot_size: int = 1        # KRX 기본 lot (Task 10 symbol_master 적용 전)

    def __post_init__(self) -> None:
        if self.per_order_max_krw <= 0:
            raise ValueError("per_order_max_krw must be > 0")
        if self.min_cash_reserve_krw < 0:
            raise ValueError("min_cash_reserve_krw must be >= 0")
        if self.default_lot_size <= 0:
            raise ValueError("default_lot_size must be > 0")


# ============================================================
# 4. 사이징 컨텍스트 (Sizing Context)
# ============================================================

@dataclass(frozen=True)
class SizingContext:
    """사이징 결정에 필요한 모든 입력.

    호출자(시그널 러너 등)가 컨텍스트를 구성하여 sizer 에 전달한다.
    """
    reference_price: Decimal       # 사이징 기준 가격 (LIMIT 가 또는 마지막 호가)
    available_cash_krw: Decimal    # 가용 현금 잔고
    config: SizingConfig
    capacity: CapacityLimits
    lot_size: Optional[int] = None  # 종목별 lot (None 이면 capacity.default_lot_size)

    def effective_lot_size(self) -> int:
        return self.lot_size if self.lot_size is not None else self.capacity.default_lot_size


# ============================================================
# 5. 사이저 (Sizer)
# ============================================================

class Sizer:
    """사이징 엔진. DRAFT OrderIntent + SizingContext → SIZED or REJECTED OrderIntent.

    본 클래스는 상태가 없는 순수 함수 집합으로 동작한다.
    (인스턴스화하여 사용하나 instance state 미보유.)
    """

    def size(
        self,
        intent: OrderIntent,
        ctx: SizingContext,
        *,
        at_utc: Optional[datetime] = None,
    ) -> OrderIntent:
        """OrderIntent 에 사이징을 적용.

        Parameters
        ----------
        intent : OrderIntent
            DRAFT 상태여야 한다.
        ctx : SizingContext
            가격, 잔고, 정책, 한도.
        at_utc : datetime, optional
            전이 시각. 미지정 시 now(UTC).

        Returns
        -------
        OrderIntent
            SIZED 또는 REJECTED 상태의 새 인스턴스.

        Notes
        -----
        본 메서드는 예외를 던지지 않는다 (fail-closed).
        모든 오류는 REJECTED 상태로 반환된다.
        """
        if at_utc is None:
            at_utc = datetime.now(timezone.utc)

        # ---- 0. 사전 검증 — DRAFT 상태인가?
        if intent.intent_state != IntentState.DRAFT:
            return self._reject(
                intent, at_utc,
                RejectionReason.VALIDATION_ERROR,
                f"sizing requires DRAFT state, got {intent.intent_state.value}",
            )

        # ---- 1. 입력 검증 — fail-closed wrap
        try:
            self._validate_inputs(ctx)
        except Exception as exc:  # noqa: BLE001
            logger.warning("sizing input validation failed: %s", exc)
            return self._reject(
                intent, at_utc,
                RejectionReason.VALIDATION_ERROR,
                f"invalid sizing input: {exc}",
            )

        # ---- 본 단계부터 모든 산술은 Decimal 로
        try:
            metadata: dict[str, Any] = {
                "policy": ctx.config.policy.value,
                "reference_price": str(ctx.reference_price),
                "available_cash_krw": str(ctx.available_cash_krw),
                "lot_size": ctx.effective_lot_size(),
            }

            # ---- 2. 원시 수량 계산
            raw_quantity = self._compute_raw_quantity(ctx)
            metadata["raw_quantity"] = raw_quantity

            if raw_quantity <= 0:
                return self._reject(
                    intent, at_utc,
                    RejectionReason.INSUFFICIENT_CAPITAL,
                    "raw quantity is 0",
                    metadata=metadata,
                )

            # ---- 3. per_order 한도 clamp
            after_per_order = self._clamp_per_order(raw_quantity, ctx)
            metadata["after_per_order_clamp"] = after_per_order

            if after_per_order <= 0:
                return self._reject(
                    intent, at_utc,
                    RejectionReason.PER_ORDER_NOTIONAL_EXCEEDED,
                    "per_order limit smaller than 1 lot at reference price",
                    metadata=metadata,
                )

            # ---- 4. min_cash 보존 clamp (BUY 시에만 의미. SELL 은 매도 대금 들어옴)
            if intent.side == Side.BUY:
                after_min_cash = self._clamp_min_cash(after_per_order, intent.side, ctx)
            else:
                after_min_cash = after_per_order
            metadata["after_min_cash_clamp"] = after_min_cash

            if after_min_cash <= 0:
                return self._reject(
                    intent, at_utc,
                    RejectionReason.INSUFFICIENT_CAPITAL,
                    "min_cash reserve cannot be preserved",
                    metadata=metadata,
                )

            # ---- 5. lot size 보정
            lot = ctx.effective_lot_size()
            after_lot_round = (after_min_cash // lot) * lot
            metadata["after_lot_round"] = after_lot_round

            if after_lot_round <= 0:
                return self._reject(
                    intent, at_utc,
                    RejectionReason.BELOW_MIN_LOT,
                    f"final quantity below lot size ({lot})",
                    metadata=metadata,
                )

            # ---- 6. 최종 메타데이터
            final_quantity = after_lot_round
            final_notional = ctx.reference_price * Decimal(final_quantity)
            estimated_fee = estimate_fee_krw(intent.side.value, final_notional)
            metadata["final_quantity"] = final_quantity
            metadata["final_notional_krw"] = str(final_notional)
            metadata["estimated_fee_krw"] = str(estimated_fee)
            if intent.side == Side.BUY:
                cash_after = ctx.available_cash_krw - final_notional - estimated_fee
                metadata["cash_after_estimated_krw"] = str(cash_after)
            metadata["min_cash_reserve_krw"] = str(ctx.capacity.min_cash_reserve_krw)
            metadata["per_order_max_krw"] = str(ctx.capacity.per_order_max_krw)

            # ---- 7. SIZED 전이
            # MARKET 주문이면 price 는 None 유지, LIMIT 이면 reference_price 적용
            updates: dict[str, Any] = {
                "quantity": final_quantity,
                "sizing_metadata": metadata,
            }
            if intent.order_type == OrderType.LIMIT and intent.price is None:
                # DRAFT 단계에서 price 미설정이었던 LIMIT 의도에 reference_price 적용
                updates["price"] = ctx.reference_price
            # arrival_price 는 의도 생성 시 결정되어야 하나, 미설정 시 reference_price 로
            if intent.arrival_price is None:
                updates["arrival_price"] = ctx.reference_price

            return intent.transition_to(
                IntentState.SIZED,
                at_utc=at_utc,
                note=f"sized via {ctx.config.policy.value}",
                **updates,
            )

        except Exception as exc:  # noqa: BLE001
            # 어떤 예외도 여기서 PASS 로 흘러 들어가지 않도록 fail-closed
            logger.exception("sizing engine internal error")
            return self._reject(
                intent, at_utc,
                RejectionReason.VALIDATION_ERROR,
                f"sizing internal error: {type(exc).__name__}",
            )

    # ============================================================
    # 6. 내부 헬퍼 (Internal Helpers)
    # ============================================================

    @staticmethod
    def _validate_inputs(ctx: SizingContext) -> None:
        """입력 검증. 실패 시 ValueError."""
        if not isinstance(ctx.reference_price, Decimal):
            raise ValueError("reference_price must be Decimal")
        if ctx.reference_price <= 0:
            raise ValueError("reference_price must be > 0")
        if not isinstance(ctx.available_cash_krw, Decimal):
            raise ValueError("available_cash_krw must be Decimal")
        if ctx.available_cash_krw <= 0:
            raise ValueError("available_cash_krw must be > 0")

    @staticmethod
    def _compute_raw_quantity(ctx: SizingContext) -> int:
        """정책별 원시 수량 계산. 최저 한도(>0) 보장 안 함 — clamp 단계 책임."""
        cfg = ctx.config
        price = ctx.reference_price

        if cfg.policy == SizingPolicy.FIXED_NOTIONAL:
            assert cfg.fixed_notional_krw is not None
            qty_dec = (cfg.fixed_notional_krw / price).quantize(
                Decimal("1"), rounding=ROUND_DOWN
            )
            return int(qty_dec)

        if cfg.policy == SizingPolicy.FIXED_QUANTITY:
            assert cfg.fixed_quantity is not None
            return cfg.fixed_quantity

        if cfg.policy == SizingPolicy.PCT_CAPITAL:
            assert cfg.pct_capital is not None
            allocated = ctx.available_cash_krw * cfg.pct_capital
            qty_dec = (allocated / price).quantize(
                Decimal("1"), rounding=ROUND_DOWN
            )
            return int(qty_dec)

        raise ValueError(f"unknown policy {cfg.policy!r}")

    @staticmethod
    def _clamp_per_order(quantity: int, ctx: SizingContext) -> int:
        """per_order_max_krw 한도 초과 시 quantity 축소."""
        if quantity <= 0:
            return 0
        notional = ctx.reference_price * Decimal(quantity)
        if notional <= ctx.capacity.per_order_max_krw:
            return quantity
        # 한도 초과 — 한도 내 최대 수량으로 재계산
        max_qty_dec = (ctx.capacity.per_order_max_krw / ctx.reference_price).quantize(
            Decimal("1"), rounding=ROUND_DOWN
        )
        return int(max_qty_dec)

    @staticmethod
    def _clamp_min_cash(quantity: int, side: Side, ctx: SizingContext) -> int:
        """min_cash 보존을 위해 BUY 수량 축소.

        SELL 은 매도 대금이 들어오므로 min_cash 보존 영향 없음 → 본 함수 호출 안 함.
        """
        if quantity <= 0:
            return 0
        notional = ctx.reference_price * Decimal(quantity)
        # 수수료 추정도 차감
        fee = estimate_fee_krw(side.value, notional)
        cash_after = ctx.available_cash_krw - notional - fee
        if cash_after >= ctx.capacity.min_cash_reserve_krw:
            return quantity
        # min_cash 보존 위반 — 수량 재계산
        # 사용 가능 현금 = available_cash - min_cash_reserve
        # 이 금액 안에 (notional + fee) 가 들어가야 함
        # fee = notional * rate (대략) → notional * (1 + rate) ≤ usable
        # 근사: usable / price 로 한 후 수수료 검증·재조정
        usable = ctx.available_cash_krw - ctx.capacity.min_cash_reserve_krw
        if usable <= 0:
            return 0
        # 첫 근사
        candidate = int((usable / ctx.reference_price).quantize(
            Decimal("1"), rounding=ROUND_DOWN
        ))
        # 수수료 포함 재검증 — 부족하면 1주씩 차감
        while candidate > 0:
            cand_notional = ctx.reference_price * Decimal(candidate)
            cand_fee = estimate_fee_krw(side.value, cand_notional)
            if (ctx.available_cash_krw - cand_notional - cand_fee) >= ctx.capacity.min_cash_reserve_krw:
                return candidate
            candidate -= 1
        return 0

    @staticmethod
    def _reject(
        intent: OrderIntent,
        at_utc: datetime,
        reason: RejectionReason,
        detail: str,
        *,
        metadata: Optional[dict[str, Any]] = None,
    ) -> OrderIntent:
        """REJECTED 전이 헬퍼."""
        updates: dict[str, Any] = {}
        if metadata is not None:
            updates["sizing_metadata"] = metadata
        return intent.transition_to(
            IntentState.REJECTED,
            at_utc=at_utc,
            note=f"sizing rejected: {reason.value}",
            rejection_reason=reason,
            rejection_detail=detail,
            **updates,
        )


__all__ = [
    "SizingPolicy",
    "SizingConfig",
    "CapacityLimits",
    "SizingContext",
    "Sizer",
]
