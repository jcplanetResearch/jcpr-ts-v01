"""공통 브로커 데이터 타입 (Shared broker data types).

모든 어댑터(KIS, 키움, NH 등)가 동일하게 사용하는 도메인 모델.
브로커별 raw 응답은 각 어댑터에서 본 모듈의 타입으로 변환된다.

설계 원칙 (Design principles):
- 모든 금액·가격은 Decimal (float 절대 금지) — P&L 정합성 보장
- 식별 정보(계좌번호, 토큰)는 마스킹된 형태로만 노출
- 모델은 가능한 한 frozen=True (immutable) — 의도치 않은 변경 방지
- KRX 시장 특수성은 어댑터에서 본 모델로 매핑

관련 모듈 (Related modules):
- src/brokers/base.py             — 본 타입을 사용하는 추상 인터페이스
- src/brokers/errors.py           — 표준화된 예외 계층
- src/execution/order_intent.py   — Task 17, OrderIntent 의 발전형
"""
from __future__ import annotations
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ============================================================
# 1. 열거형 (Enums)
# ============================================================
class Side(str, Enum):
    """거래 방향. KRX 는 long-only 가 일반적이지만 인터페이스는 양방향 지원."""
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    """주문 종류 — 어댑터에서 KRX 특수 주문(시장가, 지정가, 조건부 등)으로 매핑."""
    MARKET = "MARKET"        # 시장가 (Market order)
    LIMIT = "LIMIT"          # 지정가 (Limit order)
    # KRX 특수 (조건부지정가, 최유리지정가 등)는 추후 확장 시 여기 추가


class TimeInForce(str, Enum):
    """주문 유효 기간."""
    DAY = "DAY"              # 당일 유효 (기본)
    IOC = "IOC"              # Immediate-or-Cancel
    FOK = "FOK"              # Fill-or-Kill


class OrderStatus(str, Enum):
    """주문 상태 — 브로커별 코드는 어댑터에서 본 enum 으로 매핑."""
    PENDING_NEW = "PENDING_NEW"            # 클라이언트에서 발송, 브로커 ack 미수신
    NEW = "NEW"                            # 브로커 접수 완료
    PARTIALLY_FILLED = "PARTIALLY_FILLED"  # 부분 체결
    FILLED = "FILLED"                      # 전량 체결
    CANCELLED = "CANCELLED"                # 취소됨
    REJECTED = "REJECTED"                  # 브로커 거부
    EXPIRED = "EXPIRED"                    # 시간 만료 (DAY 등)


# ============================================================
# 2. 공용 베이스 (Common base)
# ============================================================
class _FrozenModel(BaseModel):
    """기본 immutable 모델 — 가격·잔고 등 변경 불가 데이터에 사용."""
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)


class _MutableModel(BaseModel):
    """필요 시 변경 가능한 모델 (현재는 OrderIntent 만 해당)."""
    model_config = ConfigDict(str_strip_whitespace=True)


# ============================================================
# 3. 시세 (Quote)
# ============================================================
class Quote(_FrozenModel):
    """단일 심볼 시세 스냅샷."""
    symbol: str
    bid: Decimal
    ask: Decimal
    last: Decimal
    ts: datetime
    bid_size: Optional[int] = None
    ask_size: Optional[int] = None

    @field_validator("bid", "ask", "last")
    @classmethod
    def _non_negative(cls, v: Decimal) -> Decimal:
        if v < 0:
            raise ValueError("price must be non-negative")
        return v

    @property
    def mid(self) -> Decimal:
        """중간가 (bid + ask) / 2 — 호가 양쪽이 유효할 때만 의미 있음."""
        return (self.bid + self.ask) / Decimal(2)

    @property
    def spread_bps(self) -> Optional[Decimal]:
        """호가 스프레드 (bps) — risk_limits.execution_guards 에서 사용."""
        if self.mid == 0:
            return None
        return (self.ask - self.bid) / self.mid * Decimal(10000)


# ============================================================
# 4. 포지션 / 계좌 (Position & Account)
# ============================================================
class Position(_FrozenModel):
    """보유 포지션."""
    symbol: str
    quantity: int                    # 음수=숏, 0 은 사용하지 않음 (포지션이 없으면 미반환)
    avg_price: Decimal               # 평균 진입가
    market_value: Decimal            # 현재 시장가치
    unrealized_pnl: Decimal


class Account(_FrozenModel):
    """계좌 요약.

    SECURITY: account_id_masked 만 노출. 원본 계좌번호는 어댑터 내부에서만 보관.
    """
    account_id_masked: str           # e.g. "12345***890"
    currency: str                    # ISO 4217 (e.g. "KRW", "USD")
    cash_balance: Decimal
    total_equity: Decimal

    @field_validator("account_id_masked")
    @classmethod
    def _must_be_masked(cls, v: str) -> str:
        """마스킹 형태 강제 — '***' 가 포함되어야 함."""
        if "***" not in v:
            raise ValueError(
                "account_id must be masked (contain '***'); "
                "raw account IDs must not leak through this field"
            )
        return v


# ============================================================
# 5. 주문 의도 / 응답 / 체결 (Order Intent / Ack / Fill)
# ============================================================
class OrderIntent(_MutableModel):
    """주문 의도 — 사이징·리스크게이트 통과 후 어댑터로 전달.

    client_order_id 는 idempotency key (Task 22). 동일 ID 로 두 번 전송 시
    어댑터는 첫 번째 OrderAck 를 그대로 반환하거나 거부해야 한다.
    """
    client_order_id: str = Field(min_length=1, max_length=64)
    symbol: str = Field(min_length=1)
    side: Side
    order_type: OrderType
    quantity: int = Field(gt=0)               # 항상 양수, side 가 방향 표현
    limit_price: Optional[Decimal] = None
    tif: TimeInForce = TimeInForce.DAY

    @field_validator("limit_price")
    @classmethod
    def _limit_price_positive(cls, v: Optional[Decimal]) -> Optional[Decimal]:
        """limit_price 가 주어진 경우 양수여야 한다."""
        if v is not None and v <= 0:
            raise ValueError("limit_price must be positive")
        return v

    @model_validator(mode="after")
    def _validate_order_type_price_combo(self) -> "OrderIntent":
        """LIMIT 주문에는 limit_price 필수, MARKET 주문에는 금지.

        model_validator(mode='after') 를 사용해야 limit_price 가 명시적으로
        전달되지 않은 경우(=None 기본값)에도 검증이 동작한다.
        """
        if self.order_type == OrderType.LIMIT and self.limit_price is None:
            raise ValueError("limit_price is required for LIMIT orders")
        if self.order_type == OrderType.MARKET and self.limit_price is not None:
            raise ValueError("limit_price must be None for MARKET orders")
        return self


class OrderAck(_FrozenModel):
    """브로커의 주문 접수 응답."""
    client_order_id: str
    broker_order_id: str
    accepted_at: datetime
    status: OrderStatus


class Fill(_FrozenModel):
    """체결 1건."""
    broker_order_id: str
    client_order_id: Optional[str] = None
    symbol: str
    side: Side
    quantity: int = Field(gt=0)
    price: Decimal
    fee: Decimal
    ts: datetime

    @field_validator("price", "fee")
    @classmethod
    def _non_negative(cls, v: Decimal) -> Decimal:
        if v < 0:
            raise ValueError("price/fee must be non-negative")
        return v


# ============================================================
# 6. 어댑터 메타 정보 (Adapter meta)
# ============================================================
class HealthStatus(_FrozenModel):
    """브로커 연결 상태."""
    is_healthy: bool
    latency_ms: Optional[int] = None
    note: Optional[str] = None


class RateLimitInfo(_FrozenModel):
    """브로커 레이트 리밋 정보 — 상위 모듈이 백오프 전략에 사용."""
    requests_per_second: int = Field(ge=0)
    requests_per_minute: int = Field(ge=0)
    burst_capacity: int = Field(ge=0)
