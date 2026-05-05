"""src/execution/order_intent.py — Task 17: OrderIntent 발전형.

시그널(signal) → 실제 주문(order) 변환의 중간 표현(intermediate representation).
시스템 내부 의사결정에 필요한 모든 메타데이터를 포함한다.

상태 머신 (State Machine)
------------------------
DRAFT (시그널만 있음, 수량 미정)
  ↓ sizing 적용
SIZED (수량·가격 결정)
  ↓ risk_gate 통과
RISK_CHECKED (리스크 통과)
  ↓ execution_gateway 승인
APPROVED (실행 직전)
  ↓ broker.place_order 전송 완료
SUBMITTED

REJECTED  : 어느 단계든 거부
CANCELLED : 전송 후 취소

상태 전이는 단방향(one-way). 잘못된 전이 시 ValidationError.

보안 (Security)
--------------
- __repr__ 에는 client_order_id, intent_id, signal_id 만 표시
- 시크릿·계좌번호 미포함 (계좌 정보는 어댑터 계층 책임)

정지 우선 (Stop-first)
---------------------
OrderIntent 자체는 정지 검사를 하지 않는다.
정지 우선 책임은 risk_gate (Task 19) 와 execution_gateway (Task 21) 가 담당한다.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from src.brokers.types import OrderType, Side, TimeInForce
from src.risk._decision import RejectionReason


# ============================================================
# 1. 상태 열거형 (State Enum)
# ============================================================

class IntentState(str, Enum):
    """OrderIntent 의 라이프사이클 상태."""
    DRAFT = "DRAFT"
    SIZED = "SIZED"
    RISK_CHECKED = "RISK_CHECKED"
    APPROVED = "APPROVED"
    SUBMITTED = "SUBMITTED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


# 허용된 전이 그래프 (단방향)
# REJECTED 는 어느 활성 상태에서도 도달 가능 (rejection terminal)
# CANCELLED 는 SUBMITTED 이후에만 도달 가능
_ALLOWED_TRANSITIONS: dict[IntentState, set[IntentState]] = {
    IntentState.DRAFT: {IntentState.SIZED, IntentState.REJECTED},
    IntentState.SIZED: {IntentState.RISK_CHECKED, IntentState.REJECTED},
    IntentState.RISK_CHECKED: {IntentState.APPROVED, IntentState.REJECTED},
    IntentState.APPROVED: {IntentState.SUBMITTED, IntentState.REJECTED},
    IntentState.SUBMITTED: {IntentState.CANCELLED},  # 종료 상태 직전
    IntentState.REJECTED: set(),   # 종료 상태
    IntentState.CANCELLED: set(),  # 종료 상태
}


# ============================================================
# 2. 상태 전이 이력 항목 (State Transition Log Entry)
# ============================================================

class StateTransition(BaseModel):
    """상태 전이 이벤트의 불변 기록."""
    model_config = {"frozen": True}

    from_state: IntentState
    to_state: IntentState
    at_utc: datetime = Field(description="UTC tz-aware timestamp")
    note: Optional[str] = Field(default=None, max_length=500)

    @field_validator("at_utc")
    @classmethod
    def _ensure_utc_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("at_utc must be tz-aware")
        return v.astimezone(timezone.utc)


# ============================================================
# 3. 시크릿 패턴 검사 (Secret Pattern Check)
# ============================================================

# notes 필드에 우발적으로 시크릿이 들어가지 않도록 검사
_SECRET_PATTERNS = [
    re.compile(r"(?i)\bapp[_-]?key\b\s*[:=]"),
    re.compile(r"(?i)\bapp[_-]?secret\b\s*[:=]"),
    re.compile(r"(?i)\baccess[_-]?token\b\s*[:=]"),
    re.compile(r"(?i)\bsecret[_-]?key\b\s*[:=]"),
    re.compile(r"(?i)\bpassword\b\s*[:=]"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]{20,}"),
]


def _contains_secret_pattern(text: str) -> bool:
    """notes 필드에 시크릿 패턴이 포함되어 있는지 검사."""
    return any(pat.search(text) for pat in _SECRET_PATTERNS)


# ============================================================
# 4. OrderIntent 발전형 (Advanced OrderIntent)
# ============================================================

class OrderIntent(BaseModel):
    """주문 의도(order intent) — 시그널과 실제 주문 사이의 중간 표현.

    상태 머신을 따라 sizing → risk_gate → execution_gateway 를 통과한다.
    각 단계는 본 객체의 새 사본(immutable copy)을 반환하는 방식으로 진행된다.
    """
    model_config = {
        "frozen": True,           # 불변(immutable) — 새 사본으로 진행
        "validate_assignment": True,
        "extra": "forbid",        # 알려지지 않은 필드 거부
    }

    # ------------------------------------------------------------
    # 4.1 식별자 (Identifiers)
    # ------------------------------------------------------------
    intent_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="시스템 내부 의도 추적 UUID (client_order_id 와 분리)",
    )
    client_order_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="브로커 idempotency key (Task 22)",
    )
    signal_id: Optional[str] = Field(
        default=None,
        max_length=128,
        description="출처 시그널 ID (없으면 수동 발주)",
    )
    strategy_name: Optional[str] = Field(
        default=None,
        max_length=64,
        description="전략 이름 (출력 #6 strategy attribution)",
    )

    # ------------------------------------------------------------
    # 4.2 주문 핵심 (Order Core)
    # ------------------------------------------------------------
    symbol: str = Field(..., min_length=1, max_length=16)
    side: Side
    order_type: OrderType
    quantity: int = Field(..., ge=0, description="DRAFT 단계는 0 가능, SIZED 이후 > 0")
    price: Optional[Decimal] = Field(
        default=None,
        description="LIMIT 주문 시 필수, MARKET 시 None",
    )
    time_in_force: TimeInForce = Field(default=TimeInForce.DAY)

    # ------------------------------------------------------------
    # 4.3 시점·기준 (Timing & Reference)
    # ------------------------------------------------------------
    created_at_utc: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC tz-aware. 의도 최초 생성 시각",
    )
    arrival_price: Optional[Decimal] = Field(
        default=None,
        description="의도 생성 시점의 시장 기준가 (출력 #5 슬리피지 계산 기준)",
    )

    # ------------------------------------------------------------
    # 4.4 상태 (State)
    # ------------------------------------------------------------
    intent_state: IntentState = Field(default=IntentState.DRAFT)
    state_history: tuple[StateTransition, ...] = Field(default_factory=tuple)
    rejection_reason: Optional[RejectionReason] = Field(default=None)
    rejection_detail: Optional[str] = Field(default=None, max_length=500)

    # ------------------------------------------------------------
    # 4.5 메타데이터 (Metadata)
    # ------------------------------------------------------------
    sizing_metadata: Optional[dict[str, Any]] = Field(default=None)
    notes: Optional[str] = Field(default=None, max_length=1000)

    # ============================================================
    # 5. 검증 (Validators)
    # ============================================================

    @field_validator("created_at_utc")
    @classmethod
    def _ensure_utc_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("created_at_utc must be tz-aware")
        return v.astimezone(timezone.utc)

    @field_validator("price", "arrival_price")
    @classmethod
    def _ensure_positive_price(cls, v: Optional[Decimal]) -> Optional[Decimal]:
        if v is None:
            return v
        if not isinstance(v, Decimal):
            v = Decimal(str(v))
        if v <= 0:
            raise ValueError(f"price must be positive when set, got {v}")
        return v

    @field_validator("symbol")
    @classmethod
    def _normalize_symbol(cls, v: str) -> str:
        v = v.strip().upper()
        if not v:
            raise ValueError("symbol must not be empty")
        return v

    @field_validator("client_order_id")
    @classmethod
    def _validate_client_order_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("client_order_id must not be empty")
        # 영숫자, '-', '_' 만 허용 (브로커 호환성)
        if not re.fullmatch(r"[A-Za-z0-9_-]+", v):
            raise ValueError(
                "client_order_id must contain only alphanumerics, '-', '_'"
            )
        return v

    @field_validator("notes")
    @classmethod
    def _validate_notes_no_secrets(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if _contains_secret_pattern(v):
            raise ValueError("notes appears to contain a secret pattern; refused")
        return v

    @model_validator(mode="after")
    def _validate_consistency(self) -> "OrderIntent":
        # LIMIT 주문 시 price 필수
        if self.order_type == OrderType.LIMIT and self.price is None:
            raise ValueError("LIMIT order requires price")
        # MARKET 주문 시 price 는 None 권장 (지정 시 무시되지만 명확성을 위해 거부)
        if self.order_type == OrderType.MARKET and self.price is not None:
            raise ValueError("MARKET order must not specify price")

        # SIZED 이상 상태에서는 quantity > 0
        active_after_draft = {
            IntentState.SIZED,
            IntentState.RISK_CHECKED,
            IntentState.APPROVED,
            IntentState.SUBMITTED,
        }
        if self.intent_state in active_after_draft and self.quantity <= 0:
            raise ValueError(
                f"quantity must be > 0 in state {self.intent_state.value}, "
                f"got {self.quantity}"
            )

        # REJECTED 상태에서는 rejection_reason 필수
        if self.intent_state == IntentState.REJECTED and self.rejection_reason is None:
            raise ValueError("REJECTED state requires rejection_reason")

        # 비-REJECTED 상태에서는 rejection_reason 미설정
        if self.intent_state != IntentState.REJECTED and self.rejection_reason is not None:
            raise ValueError(
                f"rejection_reason is set but state is {self.intent_state.value} "
                f"(not REJECTED)"
            )

        # 상태 이력 시간 단조성(monotonicity) 검증
        prev_ts: Optional[datetime] = None
        for tr in self.state_history:
            if prev_ts is not None and tr.at_utc < prev_ts:
                raise ValueError("state_history timestamps must be monotonically non-decreasing")
            prev_ts = tr.at_utc

        # 상태 이력의 마지막 to_state 가 현재 intent_state 와 일치해야 함
        if self.state_history:
            last = self.state_history[-1]
            if last.to_state != self.intent_state:
                raise ValueError(
                    f"state_history last to_state ({last.to_state.value}) "
                    f"!= intent_state ({self.intent_state.value})"
                )

        return self

    # ============================================================
    # 6. 상태 전이 (State Transitions)
    # ============================================================

    def transition_to(
        self,
        new_state: IntentState,
        *,
        at_utc: Optional[datetime] = None,
        note: Optional[str] = None,
        rejection_reason: Optional[RejectionReason] = None,
        rejection_detail: Optional[str] = None,
        **field_updates: Any,
    ) -> "OrderIntent":
        """새 상태로 전이된 OrderIntent 사본을 반환 (불변).

        Parameters
        ----------
        new_state : IntentState
            전이할 새 상태. _ALLOWED_TRANSITIONS 에 따라 검증됨.
        at_utc : datetime, optional
            전이 시각. 미지정 시 now(UTC).
        note : str, optional
            전이 메모.
        rejection_reason : RejectionReason, optional
            REJECTED 상태로 전이 시 필수.
        rejection_detail : str, optional
            REJECTED 시 추가 설명.
        **field_updates : dict
            함께 갱신할 필드 (예: quantity, price, sizing_metadata).

        Returns
        -------
        OrderIntent
            새 상태가 적용된 새 인스턴스.

        Raises
        ------
        ValueError
            허용되지 않은 전이, REJECTED 시 rejection_reason 누락 등.
        """
        # 허용된 전이인가?
        allowed = _ALLOWED_TRANSITIONS.get(self.intent_state, set())
        if new_state not in allowed:
            raise ValueError(
                f"Transition {self.intent_state.value} → {new_state.value} not allowed. "
                f"Allowed: {sorted(s.value for s in allowed)}"
            )

        # REJECTED 시 사유 필수
        if new_state == IntentState.REJECTED and rejection_reason is None:
            raise ValueError("transition to REJECTED requires rejection_reason")

        # 시각 결정
        if at_utc is None:
            at_utc = datetime.now(timezone.utc)
        else:
            if at_utc.tzinfo is None:
                raise ValueError("at_utc must be tz-aware")
            at_utc = at_utc.astimezone(timezone.utc)

        # 시간 단조성 검증
        if self.state_history:
            last_ts = self.state_history[-1].at_utc
            if at_utc < last_ts:
                raise ValueError(
                    f"transition timestamp {at_utc} is before last "
                    f"transition {last_ts}"
                )

        # 전이 기록
        new_transition = StateTransition(
            from_state=self.intent_state,
            to_state=new_state,
            at_utc=at_utc,
            note=note,
        )
        new_history = self.state_history + (new_transition,)

        # 갱신 필드 통합
        updates: dict[str, Any] = dict(field_updates)
        updates["intent_state"] = new_state
        updates["state_history"] = new_history
        if new_state == IntentState.REJECTED:
            updates["rejection_reason"] = rejection_reason
            if rejection_detail is not None:
                updates["rejection_detail"] = rejection_detail
        else:
            # 비-REJECTED 상태로 전이 시 기존 rejection_reason 제거
            updates.setdefault("rejection_reason", None)
            updates.setdefault("rejection_detail", None)

        # Pydantic v2 의 model_copy(update=...) 는 model_validator 를 재실행하지 않음.
        # 정합성·보안 검증을 보장하기 위해 model_validate 로 재구축한다 (fail-closed).
        merged = {**self.model_dump(), **updates}
        return self.__class__.model_validate(merged)

    # ============================================================
    # 7. 헬퍼 (Helpers)
    # ============================================================

    @property
    def is_terminal(self) -> bool:
        """종료 상태(REJECTED, CANCELLED) 인가?"""
        return self.intent_state in (IntentState.REJECTED, IntentState.CANCELLED)

    @property
    def is_active(self) -> bool:
        """활성 상태(SUBMITTED 또는 종료 전)인가?"""
        return self.intent_state == IntentState.SUBMITTED

    def notional_krw(self) -> Optional[Decimal]:
        """명목 금액 (price * quantity). LIMIT/SIZED 이상에서만 의미 있음."""
        if self.price is None or self.quantity <= 0:
            return None
        return self.price * Decimal(self.quantity)

    # ============================================================
    # 8. 보안 표현 (Security-aware Representation)
    # ============================================================

    def __repr__(self) -> str:
        """디버깅용 — 시크릿·민감 정보 미포함."""
        return (
            f"<OrderIntent intent_id={self.intent_id[:8]}... "
            f"client_order_id={self.client_order_id} "
            f"symbol={self.symbol} {self.side.value} "
            f"qty={self.quantity} "
            f"state={self.intent_state.value}>"
        )


__all__ = [
    "IntentState",
    "StateTransition",
    "OrderIntent",
]
