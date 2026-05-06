"""
시장 데이터 수집 오케스트레이션 (Market Data Ingestion Orchestrator)
====================================================================

JCPR Trading System - jcpr-ts-v01
Task 12 v0.1 메인 진입점

Source(MarketDataSource) + Store(OHLCVStore) + SymbolMaster를 통합.
(Integrates source, store, and symbol master.)

기능 (Features):
- 거래 가능 종목만 수집 (Symbol Master fail-closed)
- 멱등 upsert
- 수집 통계 보고
- is_live 검증 (실거래 모드에서 dummy 거부)

원칙 (Principles):
- 비밀 키 코드에 없음 (실 어댑터는 별도 .env에서 키 로드)
- 모든 시각 UTC tz-aware
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .market_data_source import MarketDataSource
from .ohlcv_schema import Timeframe
from .ohlcv_store import OHLCVStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IngestionReport:
    """수집 결과 보고."""
    symbol: str
    timeframe: Timeframe
    bars_fetched: int
    bars_stored: int
    start_utc: datetime
    end_utc: datetime
    source_name: str
    is_live_source: bool
    error: Optional[str] = None


class MarketDataIngester:
    """
    시장 데이터 수집기.
    (Market data ingester.)
    """

    def __init__(
        self,
        source: MarketDataSource,
        store: OHLCVStore,
        *,
        symbol_master=None,           # SymbolMaster (Task 10) — 선택, 있으면 검증
        require_live_source: bool = False,
    ):
        self._source = source
        self._store = store
        self._symbol_master = symbol_master
        self._require_live = require_live_source

        if self._require_live and not source.is_live:
            raise RuntimeError(
                f"실거래 데이터 소스 필요 (live source required) — "
                f"source={source.name}, is_live={source.is_live}"
            )

    def ingest(
        self,
        symbol: str,
        timeframe: Timeframe,
        start_utc: datetime,
        end_utc: datetime,
    ) -> IngestionReport:
        """
        지정 종목/기간/타임프레임 수집.
        (Ingest for given symbol/period/timeframe.)
        """
        # Symbol Master 검증 (있으면)
        if self._symbol_master is not None:
            if not self._symbol_master.is_tradable(symbol):
                msg = f"심볼 마스터 검증 실패 (not tradable): {symbol}"
                logger.warning(msg)
                return IngestionReport(
                    symbol=symbol, timeframe=timeframe,
                    bars_fetched=0, bars_stored=0,
                    start_utc=start_utc, end_utc=end_utc,
                    source_name=self._source.name,
                    is_live_source=self._source.is_live,
                    error=msg,
                )

        try:
            bars = list(self._source.fetch_bars(symbol, timeframe, start_utc, end_utc))
        except Exception as e:  # noqa: BLE001 - report errors instead of crashing
            logger.exception("수집 실패: symbol=%s", symbol)
            return IngestionReport(
                symbol=symbol, timeframe=timeframe,
                bars_fetched=0, bars_stored=0,
                start_utc=start_utc, end_utc=end_utc,
                source_name=self._source.name,
                is_live_source=self._source.is_live,
                error=f"{type(e).__name__}: {e}",
            )

        n_stored = self._store.upsert_bars(bars)

        return IngestionReport(
            symbol=symbol, timeframe=timeframe,
            bars_fetched=len(bars), bars_stored=n_stored,
            start_utc=start_utc, end_utc=end_utc,
            source_name=self._source.name,
            is_live_source=self._source.is_live,
            error=None,
        )

    def ingest_many(
        self,
        symbols: list[str],
        timeframe: Timeframe,
        start_utc: datetime,
        end_utc: datetime,
    ) -> list[IngestionReport]:
        """다수 종목 일괄 수집."""
        return [
            self.ingest(s, timeframe, start_utc, end_utc) for s in symbols
        ]
