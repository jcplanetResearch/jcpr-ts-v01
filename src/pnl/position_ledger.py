"""
포지션 원장 (Position Ledger)
==============================

JCPR Trading System - jcpr-ts-v01
Task 25 v0.1

Fill 시리즈 → 종목별 보유 상태 자동 누적.
(Fill series → per-symbol position auto-accumulation.)

기능 (Features):
- apply_fill: 단일 체결 반영
- apply_fills: 다수 체결 일괄 반영 (시간순 정렬)
- get / get_all: 현재 상태 조회
- history: 변경 이력 조회
- rebuild_from_fills: Fill 전체에서 ledger 재계산 (정합성 복구)

원칙 (Principles):
- 멱등성은 호출자 책임 — fill_store가 fill_id 중복 차단
- 시간 오름차순 처리 강제 (회계 정확성)
- fail-closed: 매도 > 보유 시 ValueError
- 비밀 미포함
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from ..execution.fill_store import FillStore
from ..execution.fills import Fill
from .position_state import (
    FillApplicationResult,
    PositionLogicError,
    PositionState,
    apply_fill_to_state,
)
from .position_store import PositionStore

logger = logging.getLogger(__name__)


class PositionLedger:
    """
    포지션 원장 (Position Ledger).
    """

    def __init__(self, store: PositionStore):
        self._store = store

    @property
    def store(self) -> PositionStore:
        return self._store

    # ------------------------------------------------------------------
    # Apply
    # ------------------------------------------------------------------

    def apply_fill(self, fill: Fill) -> FillApplicationResult:
        """
        체결 1건 반영.
        (Apply single fill.)

        Returns: FillApplicationResult (new_state + realized_pnl_delta)

        Raises:
            PositionLogicError: 매도 > 보유 등
        """
        # 현재 상태 조회 (없으면 빈 포지션)
        current = self._store.get(fill.symbol) or PositionState.empty(fill.symbol)

        # 시간 역행 검증 — 직전 갱신보다 이전 시각이면 경고 (정렬 위반 가능성)
        if (
            current.last_updated_utc is not None
            and fill.filled_at_utc < current.last_updated_utc
        ):
            logger.warning(
                "[ledger] 시간 역행 fill: symbol=%s, fill_at=%s < last=%s",
                fill.symbol, fill.filled_at_utc.isoformat(),
                current.last_updated_utc.isoformat(),
            )

        # 갱신
        result = apply_fill_to_state(current, fill)

        # 저장 + 이력
        self._store.upsert(result.new_state)
        self._store.append_history(
            symbol=fill.symbol,
            fill_id=fill.fill_id,
            new_state=result.new_state,
            realized_delta_krw=result.realized_pnl_delta_krw,
            timestamp_utc=fill.filled_at_utc,
        )

        logger.debug(
            "[ledger] apply: symbol=%s side=%s qty=%d → new_qty=%d, avg=%s, realized=%s",
            fill.symbol, fill.side.value, fill.quantity,
            result.new_state.quantity, result.new_state.avg_cost_krw,
            result.new_state.realized_pnl_krw,
        )
        return result

    def apply_fills(self, fills: list[Fill]) -> dict[str, PositionState]:
        """
        다수 체결 일괄 반영. 시간 오름차순 자동 정렬.
        (Apply multiple fills — auto-sorts ascending by filled_at_utc.)

        Returns: 영향받은 종목별 최종 상태.

        Raises:
            PositionLogicError: 어떤 fill이든 거부되면 예외 (그 시점까지는 저장됨)
        """
        sorted_fills = sorted(fills, key=lambda f: (f.filled_at_utc, f.fill_id))
        affected: dict[str, PositionState] = {}
        for f in sorted_fills:
            result = self.apply_fill(f)
            affected[f.symbol] = result.new_state
        return affected

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get(self, symbol: str) -> Optional[PositionState]:
        """현재 보유 상태."""
        return self._store.get(symbol)

    def get_all(self, *, only_active: bool = True) -> dict[str, PositionState]:
        """모든 포지션 (only_active=True면 quantity>0만)."""
        return self._store.get_all(only_active=only_active)

    def history(
        self,
        symbol: str,
        *,
        start_utc: Optional[datetime] = None,
        end_utc: Optional[datetime] = None,
    ) -> list[dict]:
        """포지션 변경 이력."""
        return self._store.history(symbol, start_utc=start_utc, end_utc=end_utc)

    # ------------------------------------------------------------------
    # Rebuild — Task 28 reconciliation 용
    # ------------------------------------------------------------------

    def rebuild_from_fills(
        self,
        fill_store: FillStore,
        *,
        since_utc: Optional[datetime] = None,
    ) -> dict[str, PositionState]:
        """
        Fill 전체에서 ledger 재계산.
        (Rebuild ledger from all fills.)

        주의: 기존 positions/history 모두 삭제 후 재구축.
        (WARNING: truncates existing positions/history before rebuild.)

        용도:
        - Task 28 reconciliation 발견 시 복구
        - 알고리즘 변경 후 과거 데이터 재계산
        - 디버그
        """
        logger.warning("[ledger] REBUILD 시작 — 기존 포지션/이력 모두 삭제됨")
        self._store.truncate()

        # 기간 결정
        if since_utc is None:
            # 1970년 epoch부터 (전체)
            from datetime import datetime as dt, timezone as tz
            since_utc = dt(1970, 1, 1, tzinfo=tz.utc)

        all_fills = fill_store.fetch_since(since_utc)
        logger.info("[ledger] REBUILD: %d fills 처리 시작", len(all_fills))

        affected = self.apply_fills(all_fills)
        logger.info("[ledger] REBUILD 완료: %d 종목 활성", len(affected))
        return affected

    # ------------------------------------------------------------------
    # 통계 (Aggregate Stats)
    # ------------------------------------------------------------------

    def total_realized_pnl_krw(self) -> str:
        """모든 종목의 누적 실현 손익 합계 (str로 반환 — Decimal 직렬화)."""
        from decimal import Decimal as D
        all_states = self._store.get_all(only_active=False)
        total = sum(
            (s.realized_pnl_krw for s in all_states.values()),
            start=D("0"),
        )
        return str(total)
