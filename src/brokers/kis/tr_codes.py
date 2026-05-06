"""
KIS TR 코드 매핑 (KIS Transaction Codes)
==========================================

JCPR Trading System - jcpr-ts-v01
Task 8 v0.1

KIS OpenAPI는 동일 기능에 대해 모의투자(paper)와 실거래(live)에서
다른 TR ID를 사용. 이 모듈은 환경에 따라 자동 매핑.
(Same function uses different TR IDs in paper vs live — auto-mapped here.)

⚠️ 주의 (Caution):
이 매핑은 KIS 공식 문서를 기반으로 작성되며, KIS가 ID를 변경할 수 있음.
정기적으로 https://apiportal.koreainvestment.com 에서 확인 필요.
"""

from __future__ import annotations

from .credentials import KISEnv


# ─────────────────────────────────────────────────
# TR ID 매핑 (TR ID Mapping)
# ─────────────────────────────────────────────────
# 형식: { "기능명": ("PAPER_TR", "LIVE_TR") }

_TR_MAP: dict[str, tuple[str, str]] = {
    # ─── 시세 (Market Data) — 동일 (paper/live 구분 없음) ───
    "daily_chart":   ("FHKST03010100", "FHKST03010100"),  # 국내주식 일/주/월/년 차트
    "minute_chart":  ("FHKST03010200", "FHKST03010200"),  # 국내주식 분봉
    "current_price": ("FHKST01010100", "FHKST01010100"),  # 현재가
    "orderbook":     ("FHKST01010200", "FHKST01010200"),  # 호가 (10단계)

    # ─── 계좌 (Account) — paper/live 분리 ───
    "balance":       ("VTTC8434R", "TTTC8434R"),          # 주식 잔고/평가
    "deposit":       ("VTRP6548R", "CTRP6548R"),          # 예수금 상세
    "open_orders":   ("VTTC8036R", "TTTC8036R"),          # 미체결 주문 조회

    # ─── 주문 (Orders) — paper/live 분리 ───
    "order_buy":     ("VTTC0802U", "TTTC0802U"),          # 매수 주문 (현금)
    "order_sell":    ("VTTC0801U", "TTTC0801U"),          # 매도 주문 (현금)
    "order_modify":  ("VTTC0803U", "TTTC0803U"),          # 정정/취소
    "order_inquire": ("VTTC8001R", "TTTC8001R"),          # 주문 체결 조회
}


class TRCodeError(KeyError):
    """TR 코드 조회 실패."""


def get_tr_code(function_name: str, env: KISEnv) -> str:
    """
    기능명 + 환경 → TR ID.

    Args:
        function_name: 기능 식별자 (e.g., "daily_chart", "order_buy")
        env: KISEnv (paper / live)

    Returns:
        TR ID 문자열

    Raises:
        TRCodeError: 알 수 없는 기능명
    """
    if function_name not in _TR_MAP:
        raise TRCodeError(
            f"알 수 없는 KIS 기능명 (unknown function): {function_name!r} — "
            f"등록된 항목: {sorted(_TR_MAP.keys())}"
        )
    paper_tr, live_tr = _TR_MAP[function_name]
    return live_tr if env is KISEnv.LIVE else paper_tr


def list_functions() -> list[str]:
    """등록된 기능명 목록 (디버그/문서화용)."""
    return sorted(_TR_MAP.keys())
