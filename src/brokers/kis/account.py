"""
KIS 계좌 어댑터 (KIS Account Adapter)
======================================

JCPR Trading System - jcpr-ts-v01
Task 8 v0.1

KIS OpenAPI 계좌 조회 — 잔고, 포지션, 예수금.
(KIS account inquiry — balance, positions, deposits.)

이 모듈의 출력은 Task 19 RiskContext.open_positions 형식과 호환됩니다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from .client import KISClient
from .tr_codes import get_tr_code

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PositionInfo:
    """단일 종목 보유 포지션 정보."""
    symbol: str
    quantity: int                    # 보유 수량
    available_quantity: int          # 매도 가능 수량 (예수금 기준)
    avg_price_krw: Decimal           # 평균 매입가
    current_price_krw: Decimal       # 현재가
    market_value_krw: Decimal        # 평가액
    unrealized_pnl_krw: Decimal      # 평가손익
    unrealized_pnl_pct: Decimal      # 평가손익률 (소수)

    def to_risk_context_dict(self) -> dict[str, Any]:
        """Task 19 RiskContext.open_positions 형식."""
        return {
            "quantity": self.quantity,
            "available_quantity": self.available_quantity,
            "avg_price_krw": str(self.avg_price_krw),
            "current_price_krw": str(self.current_price_krw),
            "market_value_krw": str(self.market_value_krw),
            "unrealized_pnl_krw": str(self.unrealized_pnl_krw),
        }


@dataclass(frozen=True)
class AccountSnapshot:
    """계좌 전체 스냅샷."""
    captured_at_utc: datetime
    cash_krw: Decimal                          # 예수금 (D+0)
    available_cash_krw: Decimal                # 즉시 사용 가능 (주문가능금액)
    total_evaluation_krw: Decimal              # 총평가금액
    total_purchase_krw: Decimal                # 총매입금액
    total_unrealized_pnl_krw: Decimal          # 총평가손익
    positions: dict[str, PositionInfo] = field(default_factory=dict)
    raw_summary: dict[str, Any] = field(default_factory=dict)  # KIS 원본 (디버그)

    def open_positions_dict(self) -> dict[str, dict[str, Any]]:
        """Task 19 RiskContext.open_positions 형식 dict."""
        return {sym: p.to_risk_context_dict() for sym, p in self.positions.items()}


class KISAccount:
    """KIS 계좌 조회 어댑터."""

    BALANCE_PATH = "/uapi/domestic-stock/v1/trading/inquire-balance"

    def __init__(self, client: KISClient):
        self._client = client
        self._creds = client._creds
        self._env = client._creds.env

    def fetch_account_snapshot(self) -> AccountSnapshot:
        """
        잔고 + 포지션 일괄 조회.
        (Inquire balance + positions in one call.)

        KIS 잔고 응답:
            output1: 보유 종목 리스트
            output2: 계좌 요약 [{...summary...}]
        """
        tr_id = get_tr_code("balance", self._env)
        params = {
            "CANO": self._creds.account_cano,
            "ACNT_PRDT_CD": self._creds.account_prdt,
            "AFHR_FLPR_YN": "N",          # 시간외단일가 N
            "OFL_YN": "",
            "INQR_DVSN": "02",            # 02 = 종목별
            "UNPR_DVSN": "01",            # 01 = 평균단가
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "00",            # 00 = 전일매매포함
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }

        data = self._client.request("GET", self.BALANCE_PATH, tr_id=tr_id, params=params)

        position_rows = data.get("output1") or []
        summary_rows = data.get("output2") or []
        summary = summary_rows[0] if summary_rows else {}

        positions = self._parse_positions(position_rows)

        # 계좌 요약 추출
        cash = self._dec(summary.get("dnca_tot_amt"))         # 예수금 총금액
        available_cash = self._dec(summary.get("ord_psbl_cash"))  # 주문가능 현금
        total_eval = self._dec(summary.get("tot_evlu_amt"))   # 총평가금액
        total_pur = self._dec(summary.get("pchs_amt_smtl_amt"))  # 총매입금액
        total_pnl = self._dec(summary.get("evlu_pfls_smtl_amt"))  # 총평가손익

        return AccountSnapshot(
            captured_at_utc=datetime.now(timezone.utc),
            cash_krw=cash,
            available_cash_krw=available_cash if available_cash > 0 else cash,
            total_evaluation_krw=total_eval,
            total_purchase_krw=total_pur,
            total_unrealized_pnl_krw=total_pnl,
            positions=positions,
            raw_summary=summary,
        )

    def _parse_positions(self, rows: list[dict]) -> dict[str, PositionInfo]:
        """KIS output1 → PositionInfo dict."""
        result: dict[str, PositionInfo] = {}
        for row in rows:
            try:
                code = (row.get("pdno") or "").strip()
                if not code or len(code) != 6:
                    continue
                qty = int(row.get("hldg_qty") or 0)
                if qty <= 0:
                    continue
                avail = int(row.get("ord_psbl_qty") or qty)
                avg_price = self._dec(row.get("pchs_avg_pric"))
                cur_price = self._dec(row.get("prpr"))
                market_val = self._dec(row.get("evlu_amt"))
                pnl = self._dec(row.get("evlu_pfls_amt"))
                pnl_pct = self._dec(row.get("evlu_pfls_rt")) / Decimal("100")

                result[code] = PositionInfo(
                    symbol=code,
                    quantity=qty,
                    available_quantity=avail,
                    avg_price_krw=avg_price,
                    current_price_krw=cur_price,
                    market_value_krw=market_val,
                    unrealized_pnl_krw=pnl,
                    unrealized_pnl_pct=pnl_pct,
                )
            except (ValueError, TypeError, KeyError) as e:
                logger.warning("KIS 포지션 행 파싱 실패: %s", e)
                continue
        return result

    @staticmethod
    def _dec(v: Optional[Any]) -> Decimal:
        """안전 Decimal 변환."""
        if v is None or v == "":
            return Decimal("0")
        try:
            return Decimal(str(v))
        except (ValueError, TypeError):
            return Decimal("0")
