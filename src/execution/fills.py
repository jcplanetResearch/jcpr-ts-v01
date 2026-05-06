"""
체결 데이터 모델 (Fill Data Model)
====================================

JCPR Trading System - jcpr-ts-v01
Task 24 v0.1

브로커에서 수신한 체결(execution) 정보의 표준화된 모델 + 추상 소스 인터페이스.
(Standardized fill model + abstract source interface.)

원칙 (Principles):
- 모든 datetime UTC tz-aware
- Decimal 가격 (정밀도 보존)
- frozen=True (immutable)
- fail-closed: 검증 실패 → ValueError
- 비밀 데이터 미포함
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any


class FillSide(str, Enum):
    """체결 방향 (Fill side)."""
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class Fill:
    """
    단일 체결 (One execution from broker).

    KIS 매수: tax_krw=0 강제 (매수에 거래세 없음)
    KIS 매도: tax_krw >= 0 (증권거래세 0.18~0.23%)
    """
    fill_id: str                    # 체결 고유 ID (broker fill no)
    broker_order_no: str            # 주문번호 (KIS ODNO)
    client_order_id: str            # 우리쪽 주문 ID (Task 22 idempotency)
    symbol: str
    side: FillSide
    quantity: int                   # 체결 수량
    price: Decimal                  # 체결 가격
    fee_krw: Decimal                # 수수료 (브로커 + 거래소)
    tax_krw: Decimal                # 거래세 (매도 시만)
    filled_at_utc: datetime         # 체결 시각 (브로커 시각)
    received_at_utc: datetime       # 시스템 수신 시각
    source: str                     # "kis" / "dummy" 등
    is_partial: bool = False        # 부분 체결 여부
    raw: dict[str, Any] = field(default_factory=dict)  # 원본 broker 응답

    def __post_init__(self) -> None:
        # tz-aware
        if self.filled_at_utc.tzinfo is None:
            raise ValueError(f"filled_at_utc tz-aware 필수: {self.filled_at_utc}")
        if self.received_at_utc.tzinfo is None:
            raise ValueError(f"received_at_utc tz-aware 필수: {self.received_at_utc}")

        # 필수 ID
        if not self.fill_id or not self.fill_id.strip():
            raise ValueError("fill_id 비어있음")
        if not self.broker_order_no or not self.broker_order_no.strip():
            raise ValueError("broker_order_no 비어있음")
        if not self.client_order_id or not self.client_order_id.strip():
            raise ValueError("client_order_id 비어있음")
        if not self.symbol or not self.symbol.strip():
            raise ValueError("symbol 비어있음")

        # 수량/가격 검증
        if self.quantity <= 0:
            raise ValueError(f"quantity 양수 필요: {self.quantity}")
        if self.price <= 0:
            raise ValueError(f"price 양수 필요: {self.price}")

        # 비용 음수 불가
        if self.fee_krw < 0:
            raise ValueError(f"fee_krw 음수 불가: {self.fee_krw}")
        if self.tax_krw < 0:
            raise ValueError(f"tax_krw 음수 불가: {self.tax_krw}")

        # KIS 매수에 거래세는 0이어야 (KRX 규정)
        if self.side == FillSide.BUY and self.tax_krw > 0:
            raise ValueError(
                f"매수 체결의 거래세는 0이어야 함 (KRX 규정): tax_krw={self.tax_krw}"
            )

    # ---------- 파생 지표 ----------

    def gross_amount_krw(self) -> Decimal:
        """체결 금액 (수수료/세금 제외)."""
        return self.price * Decimal(self.quantity)

    def net_amount_krw(self) -> Decimal:
        """
        실제 자금 흐름 (수수료/세금 반영).
        매수: -gross - fee  (지출)
        매도: +gross - fee - tax (수입)
        """
        gross = self.gross_amount_krw()
        if self.side == FillSide.BUY:
            return -(gross + self.fee_krw)
        return gross - self.fee_krw - self.tax_krw

    def total_cost_krw(self) -> Decimal:
        """총 비용 (fee + tax)."""
        return self.fee_krw + self.tax_krw


# ─────────────────────────────────────────────────
# 추상 인터페이스 (Abstract Source)
# ─────────────────────────────────────────────────

class FillSource(ABC):
    """
    체결 데이터 소스 추상 베이스.
    (Abstract base for fill data sources.)
    """

    name: str = "abstract"

    @property
    @abstractmethod
    def is_live(self) -> bool:
        """실거래 데이터 소스 여부."""
        raise NotImplementedError

    @abstractmethod
    def fetch_fills_since(self, since_utc: datetime) -> list[Fill]:
        """
        지정 시각 이후의 모든 체결 조회.
        (Fetch all fills since given time.)
        """
        raise NotImplementedError

    @abstractmethod
    def fetch_fills_for_order(self, broker_order_no: str) -> list[Fill]:
        """
        특정 주문의 체결 조회 (부분 체결 시 여러 건).
        (Fetch fills for a specific order — multiple if partial fills.)
        """
        raise NotImplementedError
