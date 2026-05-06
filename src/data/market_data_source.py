"""
시장 데이터 소스 추상 인터페이스 (Market Data Source Abstract Interface)
========================================================================

JCPR Trading System - jcpr-ts-v01
Task 12 v0.1

OHLCV 수집의 단일 추상 인터페이스.
(Single abstraction for OHLCV ingestion.)

구현체 (Implementations):
- DummySource: 합성 데이터 (테스트/오프라인) — is_live=False
- KISAdapter: KIS OpenAPI 어댑터 (Task 8 본격 구현 시)

원칙 (Principles):
- 모든 datetime은 UTC tz-aware
- is_live 속성으로 실거래/모의 구분 (시그널 러너가 신뢰)
- fail-closed: 예외는 호출자에 전파 (수집 실패 시 저장 안 함)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Iterable

from .ohlcv_schema import OHLCVBar, Timeframe


class MarketDataSource(ABC):
    """
    시장 데이터 소스 추상 베이스.
    (Abstract base for market data sources.)
    """

    name: str = "abstract"

    @property
    @abstractmethod
    def is_live(self) -> bool:
        """
        실거래 데이터 소스 여부.
        (Whether this is a live data source.)

        DummySource는 False 반환.
        시그널 러너는 실거래 모드에서 is_live=False 소스를 거부해야 함.
        """
        raise NotImplementedError

    @abstractmethod
    def fetch_bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        start_utc: datetime,
        end_utc: datetime,
    ) -> Iterable[OHLCVBar]:
        """
        지정 기간의 봉 데이터 조회.
        (Fetch bars for given period.)

        Args:
            symbol: KRX 종목 코드 (6자리)
            timeframe: 봉 시간 단위
            start_utc: 시작 시각 (포함, tz-aware UTC)
            end_utc: 종료 시각 (포함, tz-aware UTC)

        Yields/Returns:
            OHLCVBar (시간 오름차순 정렬)

        Raises:
            ValueError: 잘못된 입력
            RuntimeError: 데이터 소스 오류 (네트워크/인증 등)
        """
        raise NotImplementedError
