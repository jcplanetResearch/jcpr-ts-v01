"""
체결 수집 오케스트레이터 (Fill Ingester)
==========================================

JCPR Trading System - jcpr-ts-v01
Task 24 v0.1

FillSource → FillStore 자동 수집.
(Auto-ingestion: FillSource → FillStore.)

원칙:
- 멱등 (재실행 시 중복 안 됨)
- Symbol Master 검증 (선택 — 미상장 종목 거부)
- 결과 보고서 반환
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .fill_store import FillStore
from .fills import Fill, FillSource

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IngestionReport:
    """체결 수집 결과 보고."""
    fetched: int
    stored: int                  # 신규 저장 (이전에 없던 fill_id)
    duplicates: int              # 이미 존재 (멱등)
    rejected: int                # Symbol Master 검증 실패 등
    error: Optional[str] = None


class FillIngester:
    """
    체결 자동 수집기.
    """

    def __init__(
        self,
        source: FillSource,
        store: FillStore,
        *,
        symbol_master=None,         # SymbolMaster (Task 10) — 선택
    ):
        self._source = source
        self._store = store
        self._sm = symbol_master

    def ingest_since(self, since_utc: datetime) -> IngestionReport:
        """
        지정 시각 이후 모든 체결을 수집.
        (Ingest all fills since given time.)
        """
        try:
            fills = self._source.fetch_fills_since(since_utc)
        except Exception as e:  # noqa: BLE001
            logger.exception("체결 수집 실패")
            return IngestionReport(
                fetched=0, stored=0, duplicates=0, rejected=0,
                error=f"{type(e).__name__}: {e}",
            )

        return self._process_fills(fills)

    def ingest_for_order(self, broker_order_no: str) -> IngestionReport:
        """단일 주문의 체결 수집."""
        try:
            fills = self._source.fetch_fills_for_order(broker_order_no)
        except Exception as e:  # noqa: BLE001
            logger.exception("체결 수집 실패: order=%s", broker_order_no)
            return IngestionReport(
                fetched=0, stored=0, duplicates=0, rejected=0,
                error=f"{type(e).__name__}: {e}",
            )

        return self._process_fills(fills)

    def _process_fills(self, fills: list[Fill]) -> IngestionReport:
        """fills 리스트를 검증 + 저장."""
        fetched = len(fills)
        stored = 0
        duplicates = 0
        rejected = 0
        valid_fills: list[Fill] = []

        for f in fills:
            # Symbol Master 검증 (선택)
            if self._sm is not None and not self._sm.exists(f.symbol):
                logger.warning(
                    "Symbol Master에 없는 종목 — 거부: fill_id=%s symbol=%s",
                    f.fill_id, f.symbol,
                )
                rejected += 1
                continue

            # 중복 검사 (멱등)
            if self._store.has_fill_id(f.fill_id):
                duplicates += 1
                continue

            valid_fills.append(f)

        if valid_fills:
            self._store.upsert_many(valid_fills)
            stored = len(valid_fills)

        logger.info(
            "체결 수집 완료: fetched=%d, stored=%d, dup=%d, rej=%d",
            fetched, stored, duplicates, rejected,
        )
        return IngestionReport(
            fetched=fetched,
            stored=stored,
            duplicates=duplicates,
            rejected=rejected,
        )
