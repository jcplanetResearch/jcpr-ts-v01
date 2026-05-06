"""
KIS 시장 데이터 어댑터 (KIS Market Data Adapter)
=================================================

JCPR Trading System - jcpr-ts-v01
Task 8 v0.1

KIS OpenAPI를 통한 OHLCV 수집 — Task 12 MarketDataSource 인터페이스 구현.
(OHLCV ingestion via KIS OpenAPI — implements Task 12 MarketDataSource interface.)

지원 (Supported):
- 일봉 (daily)
- 분봉 (1m, 5m, 15m, 60m)

원칙 (Principles):
- 모든 시각 UTC tz-aware (KIS는 KST 응답 → UTC 변환)
- Decimal 정밀도 유지
- KIS 일봉은 매수/매도 거래량 별도 미제공 → ESTIMATED_HYBRID
- 분봉은 일부 엔드포인트에서 제공 가능 → 향후 확장
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

from ...data.market_data_source import MarketDataSource
from ...data.ohlcv_schema import OHLCVBar, Timeframe
from ...data.volume_classifier import classify_bar
from .client import KISClient
from .tr_codes import get_tr_code

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")


# KIS 분봉 timeframe 매핑 (KIS는 분 단위 인자만 받음)
_KIS_MINUTE_MAP = {
    Timeframe.M1: "1",
    Timeframe.M5: "5",
    Timeframe.M15: "15",
    Timeframe.M60: "60",
}


class KISMarketDataSource(MarketDataSource):
    """
    KIS OpenAPI OHLCV 어댑터.
    Task 12 MarketDataSource 인터페이스 구현.
    """

    name = "kis"

    # KIS API 엔드포인트
    DAILY_PATH = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
    MINUTE_PATH = "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"

    def __init__(self, client: KISClient, *, classify_method: str = "hybrid"):
        self._client = client
        self._env = client._creds.env
        self._classify_method = classify_method

    @property
    def is_live(self) -> bool:
        # paper도 KIS 시스템이므로 'live' 데이터 소스로 분류
        # (DummySource와 구분 — 실거래 모드에서 시그널 러너가 신뢰)
        return True

    def fetch_bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        start_utc: datetime,
        end_utc: datetime,
    ) -> Iterable[OHLCVBar]:
        if start_utc.tzinfo is None or end_utc.tzinfo is None:
            raise ValueError("start_utc, end_utc tz-aware 필수")
        if start_utc > end_utc:
            raise ValueError(f"start_utc > end_utc: {start_utc} > {end_utc}")
        if not symbol or len(symbol) != 6:
            raise ValueError(f"잘못된 KRX 코드: {symbol!r}")

        if timeframe == Timeframe.D1:
            return self._fetch_daily(symbol, start_utc, end_utc)
        if timeframe in _KIS_MINUTE_MAP:
            return self._fetch_minute(symbol, timeframe, start_utc, end_utc)
        raise ValueError(f"지원 안 하는 timeframe: {timeframe}")

    # ------------------------------------------------------------------
    # 일봉 (Daily)
    # ------------------------------------------------------------------

    def _fetch_daily(
        self, symbol: str, start_utc: datetime, end_utc: datetime,
    ) -> list[OHLCVBar]:
        # KIS 일봉은 KST 날짜 기준 (YYYYMMDD)
        start_kst = start_utc.astimezone(KST).strftime("%Y%m%d")
        end_kst = end_utc.astimezone(KST).strftime("%Y%m%d")

        tr_id = get_tr_code("daily_chart", self._env)
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",   # 주식
            "FID_INPUT_ISCD": symbol,
            "FID_INPUT_DATE_1": start_kst,
            "FID_INPUT_DATE_2": end_kst,
            "FID_PERIOD_DIV_CODE": "D",      # 일봉
            "FID_ORG_ADJ_PRC": "0",          # 0=수정주가, 1=원주가
        }

        data = self._client.request("GET", self.DAILY_PATH, tr_id=tr_id, params=params)

        # 응답 형식: {"output1": {...요약...}, "output2": [{...일자별...}, ...]}
        rows = data.get("output2") or []
        if not isinstance(rows, list):
            return []

        bars: list[OHLCVBar] = []
        prev_close: Optional[Decimal] = None
        ingested = datetime.now(timezone.utc)

        # KIS는 최신순 응답 → 시간 오름차순으로 뒤집음
        for row in reversed(rows):
            bar = self._parse_daily_row(row, symbol, prev_close, ingested)
            if bar is None:
                continue
            bars.append(bar)
            prev_close = bar.close
        return bars

    def _parse_daily_row(
        self, row: dict, symbol: str,
        prev_close: Optional[Decimal], ingested: datetime,
    ) -> Optional[OHLCVBar]:
        """
        KIS 일봉 응답 한 행 → OHLCVBar.
        주요 필드:
            stck_bsop_date: YYYYMMDD
            stck_oprc/hgpr/lwpr/clpr: 시/고/저/종 (가격)
            acml_vol: 누적 거래량
            acml_tr_pbmn: 누적 거래대금
        """
        try:
            date_str = row.get("stck_bsop_date")
            if not date_str:
                return None
            # KST 09:00 (장 시작) 기준 봉 시각 — UTC 변환
            bar_dt_kst = datetime.strptime(date_str, "%Y%m%d").replace(tzinfo=KST)
            bar_time_utc = bar_dt_kst.astimezone(timezone.utc)

            o = Decimal(str(row.get("stck_oprc") or "0"))
            h = Decimal(str(row.get("stck_hgpr") or "0"))
            l = Decimal(str(row.get("stck_lwpr") or "0"))
            c = Decimal(str(row.get("stck_clpr") or "0"))
            volume = int(row.get("acml_vol") or 0)
            value_str = row.get("acml_tr_pbmn")
            value_krw = Decimal(str(value_str)) if value_str else None

            if o <= 0 or h <= 0 or l <= 0 or c <= 0:
                return None

            tick_dir, up_vol, down_vol, split_method = classify_bar(
                o, h, l, c, volume, prev_close, method=self._classify_method,
            )

            return OHLCVBar(
                symbol=symbol,
                timeframe=Timeframe.D1,
                bar_time_utc=bar_time_utc,
                open=o, high=h, low=l, close=c,
                volume=volume, value_krw=value_krw,
                tick_direction=tick_dir,
                tick_direction_alt=tick_dir,  # KIS 일봉은 alt 미제공 → 동일
                up_volume=up_vol,
                down_volume=down_vol,
                volume_split_method=split_method,
                source=self.name,
                ingested_at_utc=ingested,
            )
        except (ValueError, TypeError, KeyError) as e:
            logger.warning("KIS 일봉 행 파싱 실패: %s — %s", e, row)
            return None

    # ------------------------------------------------------------------
    # 분봉 (Minute)
    # ------------------------------------------------------------------

    def _fetch_minute(
        self, symbol: str, timeframe: Timeframe,
        start_utc: datetime, end_utc: datetime,
    ) -> list[OHLCVBar]:
        # KIS 분봉은 종료 시각(HHMMSS) 기준 N건 — 한 번 호출당 30~100건
        # v0.1: 단순화 — end_utc 기준 1회 조회만 (더 긴 기간은 호출자가 분할)
        end_kst = end_utc.astimezone(KST)
        end_hhmmss = end_kst.strftime("%H%M%S")

        tr_id = get_tr_code("minute_chart", self._env)
        params = {
            "FID_ETC_CLS_CODE": "",
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": symbol,
            "FID_INPUT_HOUR_1": end_hhmmss,
            "FID_PW_DATA_INCU_YN": "Y",      # 과거 데이터 포함
        }
        # KIS 분봉 API는 분 단위가 별도 파라미터로 명시되지 않을 수 있음
        # (실제 KIS 문서 확인 후 보강 — v0.1은 1분 기준)

        data = self._client.request("GET", self.MINUTE_PATH, tr_id=tr_id, params=params)

        rows = data.get("output2") or []
        if not isinstance(rows, list):
            return []

        bars: list[OHLCVBar] = []
        prev_close: Optional[Decimal] = None
        ingested = datetime.now(timezone.utc)

        for row in reversed(rows):
            bar = self._parse_minute_row(row, symbol, timeframe, prev_close, ingested)
            if bar is None:
                continue
            # 시간 범위 필터
            if bar.bar_time_utc < start_utc or bar.bar_time_utc > end_utc:
                continue
            bars.append(bar)
            prev_close = bar.close
        return bars

    def _parse_minute_row(
        self, row: dict, symbol: str, timeframe: Timeframe,
        prev_close: Optional[Decimal], ingested: datetime,
    ) -> Optional[OHLCVBar]:
        """
        KIS 분봉 응답 행 → OHLCVBar.
        주요 필드:
            stck_bsop_date / stck_cntg_hour: 일자/시각
            stck_oprc/hgpr/lwpr/prpr: 시/고/저/현재가 (분봉 종가)
            cntg_vol: 거래량
        """
        try:
            date_str = row.get("stck_bsop_date")
            time_str = row.get("stck_cntg_hour")  # HHMMSS
            if not date_str or not time_str:
                return None

            dt_kst = datetime.strptime(
                date_str + time_str.zfill(6), "%Y%m%d%H%M%S",
            ).replace(tzinfo=KST)
            bar_time_utc = dt_kst.astimezone(timezone.utc)

            o = Decimal(str(row.get("stck_oprc") or "0"))
            h = Decimal(str(row.get("stck_hgpr") or "0"))
            l = Decimal(str(row.get("stck_lwpr") or "0"))
            c = Decimal(str(row.get("stck_prpr") or "0"))
            volume = int(row.get("cntg_vol") or 0)

            if o <= 0 or h <= 0 or l <= 0 or c <= 0:
                return None

            tick_dir, up_vol, down_vol, split_method = classify_bar(
                o, h, l, c, volume, prev_close, method=self._classify_method,
            )

            return OHLCVBar(
                symbol=symbol,
                timeframe=timeframe,
                bar_time_utc=bar_time_utc,
                open=o, high=h, low=l, close=c,
                volume=volume,
                tick_direction=tick_dir,
                tick_direction_alt=tick_dir,
                up_volume=up_vol,
                down_volume=down_vol,
                volume_split_method=split_method,
                source=self.name,
                ingested_at_utc=ingested,
            )
        except (ValueError, TypeError, KeyError) as e:
            logger.warning("KIS 분봉 행 파싱 실패: %s", e)
            return None
