"""
KIS 호가 어댑터 (KIS Quote Adapter)
====================================

JCPR Trading System - jcpr-ts-v01
Task 8 v0.1

KIS OpenAPI 호가 (10단계) — Task 13 QuoteSource 인터페이스 구현.
(KIS 10-level orderbook — implements Task 13 QuoteSource interface.)

원칙:
- captured_at_utc: KIS가 응답에 시각을 주면 그 값, 없으면 received_at_utc와 동일
- 호가 정합성: ask >= bid 보장 (위반 시 QuoteSnapshot이 거부 — fail-closed)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
from zoneinfo import ZoneInfo

from ...data.quote_schema import DepthLevel, QuoteSnapshot
from ...data.quote_source import QuoteSource
from .client import KISClient
from .tr_codes import get_tr_code

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")


class KISQuoteSource(QuoteSource):
    """KIS OpenAPI 호가 어댑터."""

    name = "kis_quote"

    QUOTE_PATH = "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn"

    def __init__(self, client: KISClient):
        self._client = client
        self._env = client._creds.env

    @property
    def is_live(self) -> bool:
        return True

    def snapshot(self, symbol: str) -> QuoteSnapshot:
        if not symbol or len(symbol) != 6:
            raise ValueError(f"잘못된 KRX 코드: {symbol!r}")

        tr_id = get_tr_code("orderbook", self._env)
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": symbol,
        }
        data = self._client.request("GET", self.QUOTE_PATH, tr_id=tr_id, params=params)

        # KIS 응답: output1 = 호가/잔량, output2 = 예상체결
        output1 = data.get("output1") or {}
        if not output1:
            raise RuntimeError(f"KIS 호가 응답 비어있음: {symbol}")

        return self._parse_orderbook(symbol, output1)

    def _parse_orderbook(self, symbol: str, o1: dict) -> QuoteSnapshot:
        """
        KIS output1 필드 (10단계 호가):
            askp1 ~ askp10: 매도호가 1~10
            bidp1 ~ bidp10: 매수호가 1~10
            askp_rsqn1 ~ askp_rsqn10: 매도잔량 1~10
            bidp_rsqn1 ~ bidp_rsqn10: 매수잔량 1~10
            stck_prpr: 현재가
            aspr_acpt_hour: 호가 접수 시각 (HHMMSS)
        """
        now_utc = datetime.now(timezone.utc)

        # 호가 시각 (있으면)
        captured_at = now_utc
        time_str = o1.get("aspr_acpt_hour")
        if time_str and len(str(time_str)) == 6:
            try:
                today_kst = now_utc.astimezone(KST).date()
                t = datetime.strptime(str(time_str), "%H%M%S").time()
                captured_kst = datetime.combine(today_kst, t).replace(tzinfo=KST)
                captured_at = captured_kst.astimezone(timezone.utc)
            except (ValueError, TypeError):
                pass

        # Best bid/ask (level 1)
        try:
            best_bid = Decimal(str(o1.get("bidp1") or "0"))
            best_ask = Decimal(str(o1.get("askp1") or "0"))
            best_bid_size = int(o1.get("bidp_rsqn1") or 0)
            best_ask_size = int(o1.get("askp_rsqn1") or 0)
        except (ValueError, TypeError) as e:
            raise RuntimeError(f"KIS 호가 best 파싱 실패: {e}") from e

        # 10단계 depth — KIS는 매수/매도가 별도이므로 단순 통합 표현
        # KRX 호가창은 매도 1~10 (오름차순), 매수 1~10 (내림차순)
        # 통합 표현: level은 1~10이고, level별 (price, bid_size, ask_size) 조합
        # 여기서는 매도 호가의 가격을 level의 가격으로 사용 (보수적)
        depth: list[DepthLevel] = []
        for i in range(1, 11):
            try:
                ask_price = Decimal(str(o1.get(f"askp{i}") or "0"))
                bid_price = Decimal(str(o1.get(f"bidp{i}") or "0"))
                ask_size = int(o1.get(f"askp_rsqn{i}") or 0)
                bid_size = int(o1.get(f"bidp_rsqn{i}") or 0)
            except (ValueError, TypeError):
                continue

            # KIS는 빈 호가도 0으로 채워서 응답 — 둘 다 0이면 skip
            if ask_price <= 0 and bid_price <= 0:
                continue

            # 가격은 매도호가 우선, 없으면 매수호가 (대표 가격)
            level_price = ask_price if ask_price > 0 else bid_price
            depth.append(DepthLevel(
                level=i,
                price=level_price,
                bid_size=bid_size,
                ask_size=ask_size,
            ))

        last_trade = None
        prpr = o1.get("stck_prpr")
        if prpr:
            try:
                last_trade = Decimal(str(prpr))
            except (ValueError, TypeError):
                pass

        return QuoteSnapshot(
            symbol=symbol,
            captured_at_utc=captured_at,
            received_at_utc=now_utc,
            best_bid=best_bid,
            best_ask=best_ask,
            best_bid_size=best_bid_size,
            best_ask_size=best_ask_size,
            depth_levels=tuple(depth),
            last_trade_price=last_trade,
            source=self.name,
            is_live_source=True,
        )
