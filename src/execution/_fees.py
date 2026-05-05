"""src/execution/_fees.py — KRX 수수료·세금 추정 (KRX fee/tax estimation).

본 모듈은 사이징(sizing) 단계에서 사용할 **보수적 수수료 추정**을 제공한다.
실제 수수료는 체결(fill) 수신 후 Task 27 (slippage) 에서 정정한다.

[verify] KRX 수수료·세금 정책은 변경될 수 있으며, 다음 값은
        2026년 5월 시점의 일반적 KIS 모의투자 추정치이다.
        프로덕션 진입 전 KIS 공시·국세청 공시로 재검증 필요.

Constants
---------
- KRX_BUY_FEE_RATE : 매수 수수료율 (브로커 수수료, %)
- KRX_SELL_FEE_RATE: 매도 수수료율 (브로커 수수료, %)
- KRX_SELL_TAX_RATE: 매도 거래세율 (증권거래세 + 농어촌특별세 합산, %)

본 모듈은 외부 의존성이 없으며, Decimal 산술로 부동소수점 오차를 차단한다.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Literal

# ============================================================
# 상수 (Constants) — [verify] 정기 검증 필요
# ============================================================

# 보수적(conservative) 추정: 실제보다 약간 크게.
# 사이징에서 부족분(shortfall)을 방지하기 위함.
KRX_BUY_FEE_RATE = Decimal("0.00015")   # 0.015% 매수 수수료
KRX_SELL_FEE_RATE = Decimal("0.00015")  # 0.015% 매도 수수료
KRX_SELL_TAX_RATE = Decimal("0.0020")   # 0.20% 매도 거래세 (코스피 기준)

# 안전 여유 (safety margin) — 추정 오차 흡수
SAFETY_MARGIN_RATE = Decimal("0.0005")  # 0.05% 추가 여유


def estimate_fee_krw(
    side: Literal["BUY", "SELL"],
    notional_krw: Decimal,
) -> Decimal:
    """KRX 매매 수수료·세금의 보수적 추정.

    Parameters
    ----------
    side : "BUY" or "SELL"
        매매 방향
    notional_krw : Decimal
        명목 거래금액 (KRW). 양수여야 한다.

    Returns
    -------
    Decimal
        수수료 + 세금 + 안전 여유 (KRW). 항상 양수.
        반환 단위는 KRW (원), 소수점은 ROUND_HALF_UP.

    Raises
    ------
    ValueError
        notional_krw 가 양수가 아니거나, side 가 BUY/SELL 이 아닌 경우.

    Notes
    -----
    - 보수적 추정 — 실제 수수료보다 약간 크게 산출됨
    - 매수 시: 브로커 수수료만
    - 매도 시: 브로커 수수료 + 거래세
    - 양쪽 모두 안전 여유(0.05%) 추가
    - 실제 수수료는 체결 후 정정 (Task 27)
    """
    if not isinstance(notional_krw, Decimal):
        notional_krw = Decimal(str(notional_krw))
    if notional_krw <= 0:
        raise ValueError(f"notional_krw must be positive, got {notional_krw}")
    if side not in ("BUY", "SELL"):
        raise ValueError(f"side must be 'BUY' or 'SELL', got {side!r}")

    if side == "BUY":
        rate = KRX_BUY_FEE_RATE + SAFETY_MARGIN_RATE
    else:  # SELL
        rate = KRX_SELL_FEE_RATE + KRX_SELL_TAX_RATE + SAFETY_MARGIN_RATE

    fee = notional_krw * rate
    # KRW 는 정수 단위로 반올림
    return fee.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


__all__ = [
    "KRX_BUY_FEE_RATE",
    "KRX_SELL_FEE_RATE",
    "KRX_SELL_TAX_RATE",
    "SAFETY_MARGIN_RATE",
    "estimate_fee_krw",
]
