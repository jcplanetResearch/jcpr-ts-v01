"""
KRX 호가단위 헬퍼 (KRX Tick Size Helper)
========================================

JCPR Trading System - jcpr-ts-v01
Task 18 v0.2 보조 모듈

KRX 가격대별 호가단위에 맞춰 주문가를 정렬합니다.
(Aligns order prices to KRX tick size by price band.)

원칙 (Principles):
- fail-closed: 알 수 없는 가격대는 거부 (unknown band -> reject)
- 비밀 데이터 없음 (no secrets)
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Literal

# KRX 유가증권시장(KOSPI) / 코스닥(KOSDAQ) 공통 호가단위 (2023-01-25 개정 기준)
# (KRX KOSPI/KOSDAQ unified tick size, revised 2023-01-25)
# 가격대 (price band, KRW) -> 호가단위 (tick, KRW)
_TICK_TABLE_STOCK: list[tuple[int, int]] = [
    # (상한 미만, tick)  -- "less than upper bound"
    (2_000,       1),
    (5_000,       5),
    (20_000,     10),
    (50_000,     50),
    (200_000,   100),
    (500_000,   500),
    (10**12,  1_000),  # 500,000원 이상
]

# ETF/ETN 호가단위 (전 가격대 5원 단위)
_TICK_ETF: int = 5

Side = Literal["buy", "sell"]
InstrumentType = Literal["stock", "etf", "etn"]


@dataclass(frozen=True)
class TickAlignment:
    """호가 정렬 결과 (Tick alignment result)."""
    original_price: Decimal
    aligned_price: Decimal
    tick_size: int
    method: str  # "round_down" | "round_half_up"
    instrument_type: InstrumentType


def get_tick_size(price: Decimal | int | float, instrument_type: InstrumentType = "stock") -> int:
    """
    가격과 상품유형에 맞는 호가단위 반환.
    (Return tick size for given price and instrument type.)

    Raises:
        ValueError: 가격이 음수이거나 상품유형이 알 수 없는 경우 (fail-closed)
    """
    p = Decimal(str(price))
    if p < 0:
        raise ValueError(f"가격은 음수일 수 없음 (price cannot be negative): {p}")

    if instrument_type in ("etf", "etn"):
        return _TICK_ETF

    if instrument_type == "stock":
        for upper, tick in _TICK_TABLE_STOCK:
            if p < upper:
                return tick
        # 이론상 도달 불가 (마지막 항목이 10^12 상한)
        raise ValueError(f"호가 테이블 범위 초과 (price out of tick table range): {p}")

    raise ValueError(f"알 수 없는 상품유형 (unknown instrument type): {instrument_type}")


def align_price_to_tick(
    price: Decimal | int | float,
    side: Side,
    instrument_type: InstrumentType = "stock",
    *,
    conservative: bool = True,
) -> TickAlignment:
    """
    주문가를 호가단위에 맞춰 정렬.
    (Align order price to tick size.)

    conservative=True (기본):
        - 매수(buy): 내림(round down) — 더 낮은 가격으로 정렬 (체결 안 될 수 있으나 비용 보수적)
        - 매도(sell): 올림(round up) — 더 높은 가격으로 정렬 (체결 안 될 수 있으나 수익 보수적)
        보수적 정렬은 fail-closed 원칙에 부합 (불리한 체결 방지).
        (Conservative alignment aligns with fail-closed principle.)

    conservative=False:
        반올림 (round half up) - 가까운 호가로 정렬

    Raises:
        ValueError: 잘못된 입력 (invalid input)
    """
    p = Decimal(str(price))
    if p <= 0:
        raise ValueError(f"가격은 양수여야 함 (price must be positive): {p}")
    if side not in ("buy", "sell"):
        raise ValueError(f"잘못된 side: {side}")

    tick = Decimal(get_tick_size(p, instrument_type))

    if conservative:
        if side == "buy":
            # 내림 (정수배수 중 가장 가까운 작은 값)
            aligned = (p // tick) * tick
            method = "round_down(buy_conservative)"
        else:
            # 올림
            quotient = p / tick
            if quotient == quotient.to_integral_value():
                aligned = quotient * tick
            else:
                aligned = ((p // tick) + 1) * tick
            method = "round_up(sell_conservative)"
    else:
        # 반올림
        aligned = (p / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick
        method = "round_half_up"

    if aligned <= 0:
        raise ValueError(
            f"정렬된 가격이 0 이하 (aligned price <= 0): orig={p}, tick={tick}, aligned={aligned}"
        )

    return TickAlignment(
        original_price=p,
        aligned_price=aligned,
        tick_size=int(tick),
        method=method,
        instrument_type=instrument_type,
    )


def is_aligned(price: Decimal | int | float, instrument_type: InstrumentType = "stock") -> bool:
    """가격이 이미 호가단위에 정렬되어 있는지 확인."""
    p = Decimal(str(price))
    tick = Decimal(get_tick_size(p, instrument_type))
    return (p % tick) == 0
