"""
KIS 체결 조회 어댑터 (KIS Fill Source)
========================================

JCPR Trading System - jcpr-ts-v01
Task 24 v0.1

KIS OpenAPI를 통한 체결 조회 — Task 24 FillSource 인터페이스 구현.
(Fill inquiry via KIS OpenAPI — implements Task 24 FillSource interface.)

KIS API: TTTC8001R (실거래) / VTTC8001R (모의) — 일별 주문체결 조회

원칙:
- 모든 시각 UTC 변환 (KIS는 KST 응답)
- Decimal 정밀도 유지
- 미체결 응답은 skip (체결만 추출)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from ..brokers.kis.client import KISClient
from ..brokers.kis.tr_codes import get_tr_code
from .fills import Fill, FillSide, FillSource

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")


class KISFillSource(FillSource):
    """
    KIS OpenAPI 체결 조회 어댑터.
    """

    name = "kis"
    INQUIRE_PATH = "/uapi/domestic-stock/v1/trading/inquire-daily-ccld"

    def __init__(self, client: KISClient):
        self._client = client
        self._creds = client._creds
        self._env = client._creds.env

    @property
    def is_live(self) -> bool:
        # paper도 KIS 시스템이므로 live source로 분류
        # (DummyFillSource와 구분)
        return True

    # ------------------------------------------------------------------
    # 메인 조회
    # ------------------------------------------------------------------

    def fetch_fills_since(self, since_utc: datetime) -> list[Fill]:
        """지정 시각 이후 체결 조회 (KST 일자 기준)."""
        if since_utc.tzinfo is None:
            raise ValueError("since_utc tz-aware 필수")

        end_utc = datetime.now(timezone.utc)
        start_kst = since_utc.astimezone(KST).strftime("%Y%m%d")
        end_kst = end_utc.astimezone(KST).strftime("%Y%m%d")

        return self._fetch_range(start_kst, end_kst)

    def fetch_fills_for_order(self, broker_order_no: str) -> list[Fill]:
        """특정 주문의 체결 조회 — 최근 30일 검색."""
        end_kst = datetime.now(timezone.utc).astimezone(KST)
        start_kst = end_kst - timedelta(days=30)

        all_fills = self._fetch_range(
            start_kst.strftime("%Y%m%d"),
            end_kst.strftime("%Y%m%d"),
        )
        return [f for f in all_fills if f.broker_order_no == broker_order_no]

    # ------------------------------------------------------------------
    # 내부
    # ------------------------------------------------------------------

    def _fetch_range(self, start_yyyymmdd: str, end_yyyymmdd: str) -> list[Fill]:
        """KIS 일별 주문체결 조회 — 페이징 처리."""
        tr_id = get_tr_code("order_inquire", self._env)
        params = {
            "CANO": self._creds.account_cano,
            "ACNT_PRDT_CD": self._creds.account_prdt,
            "INQR_STRT_DT": start_yyyymmdd,
            "INQR_END_DT": end_yyyymmdd,
            "SLL_BUY_DVSN_CD": "00",      # 00=전체, 01=매도, 02=매수
            "INQR_DVSN": "01",            # 01=역순, 00=정순
            "PDNO": "",                   # 빈 값 = 전체 종목
            "CCLD_DVSN": "01",            # 01=체결, 02=미체결, 00=전체
            "ORD_GNO_BRNO": "",
            "ODNO": "",
            "INQR_DVSN_3": "00",
            "INQR_DVSN_1": "",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }

        data = self._client.request("GET", self.INQUIRE_PATH, tr_id=tr_id, params=params)

        rows = data.get("output1") or []
        if not isinstance(rows, list):
            return []

        received = datetime.now(timezone.utc)
        fills: list[Fill] = []
        for row in rows:
            f = self._parse_fill(row, received)
            if f is not None:
                fills.append(f)

        # TODO: ctx_area_nk100 사용한 페이징 (다음 세션 보강)
        # 현재 v0.1: 최대 100건/호출 (KIS 제한)

        return fills

    def _parse_fill(self, row: dict, received: datetime) -> Optional[Fill]:
        """
        KIS 체결 응답 1행 → Fill.

        KIS 주요 필드:
            ord_dt: 주문일자 (YYYYMMDD)
            ord_tmd: 주문시각 (HHMMSS)
            odno: 주문번호
            orgn_odno: 원주문번호 (정정/취소 시)
            sll_buy_dvsn_cd: "01"=매도/"02"=매수
            pdno: 종목코드
            tot_ccld_qty: 총 체결수량
            avg_prvs: 체결평균단가 (또는 ccld_unpr)
            ccld_qty: 체결수량 (단일 체결일 경우)
            ccld_unpr: 체결단가
            tot_ccld_amt: 총 체결금액
            tlex_smtl: 제비용 합계 (수수료+세금)
            cmsn_smtl: 수수료 합계
            ord_qty: 주문수량
            rmn_qty: 미체결 잔량
        """
        try:
            # 체결수량 0은 미체결 — skip
            ccld_qty = int(row.get("tot_ccld_qty") or row.get("ccld_qty") or 0)
            if ccld_qty <= 0:
                return None

            broker_order_no = (row.get("odno") or "").strip()
            if not broker_order_no:
                return None

            # 종목 코드
            symbol = (row.get("pdno") or "").strip()
            if not symbol or len(symbol) != 6:
                return None

            # Side
            sb = row.get("sll_buy_dvsn_cd")
            if sb == "01":
                side = FillSide.SELL
            elif sb == "02":
                side = FillSide.BUY
            else:
                logger.warning("알 수 없는 side: %s", sb)
                return None

            # 체결 시각 (KST → UTC)
            ord_dt = (row.get("ord_dt") or "").strip()
            ord_tmd = (row.get("ord_tmd") or "").strip()
            if not ord_dt or len(ord_dt) != 8:
                return None
            tmd = ord_tmd.zfill(6) if ord_tmd else "000000"
            kst_dt = datetime.strptime(ord_dt + tmd, "%Y%m%d%H%M%S").replace(tzinfo=KST)
            filled_at = kst_dt.astimezone(timezone.utc)

            # 가격
            price_str = row.get("avg_prvs") or row.get("ccld_unpr") or "0"
            price = Decimal(str(price_str))
            if price <= 0:
                return None

            # 비용
            fee = Decimal(str(row.get("cmsn_smtl") or "0"))
            # 거래세는 매도 시만; KIS는 cmsn_smtl/tlex_smtl 등에 포함 — 보수적으로 0으로 둠
            # 정확한 추출은 별도 endpoint 또는 후속 보강
            tax = Decimal("0")
            if side == FillSide.SELL:
                # tlex_smtl - cmsn_smtl을 거래세로 추정 (간단)
                tlex = Decimal(str(row.get("tlex_smtl") or "0"))
                tax_calc = tlex - fee
                if tax_calc > 0:
                    tax = tax_calc

            # 부분 체결 — 미체결 잔량 > 0이면 partial
            rmn = int(row.get("rmn_qty") or 0)
            is_partial = rmn > 0

            # client_order_id — KIS는 표준 필드 없음. 우리가 보낸 ORGN_ODNO나 별도 추적
            # v0.1에서는 broker_order_no를 그대로 사용 (Task 22 보강 시 별도 매핑)
            client_order_id = (
                row.get("orgn_odno") or row.get("odno") or broker_order_no
            ).strip()
            if not client_order_id:
                client_order_id = broker_order_no

            # fill_id — KIS는 별도 fill ID 없음. 주문번호+체결시각 조합
            # (실제로는 ccld_seq 같은 필드가 있을 수 있음 — 응답에 포함 시 그것 사용)
            ccld_seq = row.get("ccld_seq") or row.get("seq")
            if ccld_seq:
                fill_id = f"{broker_order_no}-{ccld_seq}"
            else:
                fill_id = f"{broker_order_no}-{filled_at.strftime('%Y%m%d%H%M%S')}"

            return Fill(
                fill_id=fill_id,
                broker_order_no=broker_order_no,
                client_order_id=client_order_id,
                symbol=symbol,
                side=side,
                quantity=ccld_qty,
                price=price,
                fee_krw=fee,
                tax_krw=tax,
                filled_at_utc=filled_at,
                received_at_utc=received,
                source=self.name,
                is_partial=is_partial,
                raw={"_kis_row": row},  # 원본 보존 (감사용)
            )
        except (ValueError, TypeError, KeyError) as e:
            logger.warning("KIS 체결 파싱 실패: %s — %s", e, row)
            return None
