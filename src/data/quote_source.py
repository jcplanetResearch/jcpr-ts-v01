"""
호가 데이터 소스 추상 인터페이스 (Quote Source Abstract Interface)
====================================================================

JCPR Trading System - jcpr-ts-v01
Task 13 v0.1
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .quote_schema import QuoteSnapshot


class QuoteSource(ABC):
    """
    호가 데이터 소스 추상 베이스.
    (Abstract base for quote data sources.)
    """

    name: str = "abstract"

    @property
    @abstractmethod
    def is_live(self) -> bool:
        """실거래 데이터 소스 여부."""
        raise NotImplementedError

    @abstractmethod
    def snapshot(self, symbol: str) -> QuoteSnapshot:
        """
        현재 호가 스냅샷 1건 조회.
        (Fetch one current quote snapshot.)

        Raises:
            ValueError: 잘못된 심볼
            RuntimeError: 데이터 소스 오류
        """
        raise NotImplementedError
