"""
종목 거래 상태 (Symbol Trading Status)
======================================

JCPR Trading System - jcpr-ts-v01
Task 10 v0.1 보조 모듈

거래 가능 여부 판정 기준 (Trading eligibility criteria).
fail-closed: 알 수 없는 상태는 거래 불가로 처리.
(Unknown status treated as non-tradable.)
"""

from __future__ import annotations

from enum import Enum


class SymbolStatus(str, Enum):
    """종목 거래 상태 (Trading status of a symbol)."""

    ACTIVE = "active"          # 정상 거래 가능 (normal trading)
    HALTED = "halted"          # 거래정지 (temporary halt)
    SUSPENDED = "suspended"    # 매매거래정지 (suspended by exchange)
    DELISTED = "delisted"      # 상장폐지 (delisted)

    def is_tradable(self) -> bool:
        """거래 가능 여부. ACTIVE 이외는 모두 불가 (fail-closed)."""
        return self is SymbolStatus.ACTIVE


class Market(str, Enum):
    """KRX 시장 구분."""

    KOSPI = "KOSPI"
    KOSDAQ = "KOSDAQ"
    KONEX = "KONEX"


class InstrumentType(str, Enum):
    """상품 유형 — Task 18 tick_size.py 의 InstrumentType Literal과 호환."""

    STOCK = "stock"
    ETF = "etf"
    ETN = "etn"


class TickPolicy(str, Enum):
    """
    호가단위 정책 식별자.
    실제 정렬 로직은 src/execution/tick_size.py에 위임.
    (Actual alignment logic delegated to tick_size.py.)
    """

    KRX_STOCK = "krx_stock"   # 가격대별 단계적 호가
    KRX_ETF = "krx_etf"       # 전 가격대 5원 단위
