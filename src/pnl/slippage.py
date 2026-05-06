"""
슬리피지 + 수수료 분석 (Slippage & Fee Analysis)
=================================================

JCPR Trading System - jcpr-ts-v01
Task 27 v0.1

주문 의도(intent price) vs 실제 체결가(fill price) 분석.
(Intent vs actual fill comparison.)

핵심 정의 (Definitions):
    BUY slippage_krw  = avg_fill_price - intent_price   (+불리, -유리)
    SELL slippage_krw = intent_price - avg_fill_price   (+불리, -유리)

    slippage_bps      = abs_slippage / intent_price * 10000

    cost_impact_bps   = (fees + taxes) / (qty * intent_price) * 10000

    total_friction_bps = slippage_bps + cost_impact_bps

원칙 (Principles):
- 부분 체결은 VWAP-style 평균으로 합산
- 단위는 KRW (Decimal), bps (Decimal, 소수)
- 모든 datetime UTC tz-aware
- 비밀 미포함
"""

from __future__ import annotations

import json
import logging
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Optional

from ..execution.fill_store import FillStore
from ..execution.fills import Fill, FillSide

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SlippageRecord:
    """단일 실행(execution_id)의 슬리피지/비용 분석."""
    execution_id: str
    signal_id: Optional[str]
    broker_order_no: str
    symbol: str
    side: str                          # "buy" / "sell"
    intent_price_krw: Decimal
    intent_quantity: int

    # 체결 합산 (부분 체결 → VWAP)
    filled_quantity: int
    avg_fill_price_krw: Decimal
    fill_count: int

    # Slippage
    abs_slippage_krw: Decimal          # +/- 부호 있음 (양수=불리)
    slippage_bps: Decimal              # 부호 있음
    is_unfavorable: bool

    # Cost
    total_fees_krw: Decimal
    total_taxes_krw: Decimal
    cost_impact_bps: Decimal           # 항상 양수 (비용)

    # Total friction
    total_friction_bps: Decimal        # slippage_bps + cost_impact_bps

    # 메타
    intent_at_utc: datetime
    last_fill_at_utc: datetime
    is_partial: bool                   # 의도 수량 미달

    def to_dict(self) -> dict:
        return {
            "execution_id": self.execution_id,
            "signal_id": self.signal_id,
            "broker_order_no": self.broker_order_no,
            "symbol": self.symbol,
            "side": self.side,
            "intent_price_krw": str(self.intent_price_krw),
            "intent_quantity": self.intent_quantity,
            "filled_quantity": self.filled_quantity,
            "avg_fill_price_krw": str(self.avg_fill_price_krw),
            "fill_count": self.fill_count,
            "abs_slippage_krw": str(self.abs_slippage_krw),
            "slippage_bps": str(self.slippage_bps),
            "is_unfavorable": self.is_unfavorable,
            "total_fees_krw": str(self.total_fees_krw),
            "total_taxes_krw": str(self.total_taxes_krw),
            "cost_impact_bps": str(self.cost_impact_bps),
            "total_friction_bps": str(self.total_friction_bps),
            "intent_at_utc": self.intent_at_utc.isoformat(),
            "last_fill_at_utc": self.last_fill_at_utc.isoformat(),
            "is_partial": self.is_partial,
        }


# ─────────────────────────────────────────────────
# Analyzer
# ─────────────────────────────────────────────────

class SlippageAnalyzer:
    """
    슬리피지 + 수수료 분석기.

    Args:
        fill_store: Task 24 FillStore — 체결 데이터 소스
    """

    def __init__(self, fill_store: FillStore):
        self._fill_store = fill_store

    # ------------------------------------------------------------------
    # 단일 실행 분석
    # ------------------------------------------------------------------

    def analyze_execution(
        self,
        *,
        execution_id: str,
        intent_price_krw: Decimal,
        intent_quantity: int,
        broker_order_no: str,
        side: str,
        symbol: str,
        intent_at_utc: datetime,
        signal_id: Optional[str] = None,
    ) -> Optional[SlippageRecord]:
        """
        단일 주문에 대한 슬리피지 분석.
        체결이 없으면 None.

        Args:
            execution_id: Task 21 ExecutionResult.execution_id
            intent_price_krw: aligned_price (Task 21에서 정해진 의도가)
            intent_quantity: 의도 수량
            broker_order_no: Task 8 OrderResponse.broker_order_no
            side: "buy" / "sell"
            symbol: 종목 코드
            intent_at_utc: 의도 시각 (Task 21 started_at)
            signal_id: 시그널 ID (있으면)

        Returns:
            SlippageRecord | None (체결 없을 시)
        """
        if intent_at_utc.tzinfo is None:
            raise ValueError("intent_at_utc tz-aware 필수")
        if intent_price_krw <= 0:
            raise ValueError(f"intent_price_krw 양수 필요: {intent_price_krw}")
        if intent_quantity <= 0:
            raise ValueError(f"intent_quantity 양수 필요: {intent_quantity}")
        if side not in ("buy", "sell"):
            raise ValueError(f"side는 'buy' 또는 'sell': {side!r}")

        # 해당 주문의 모든 fill 조회 (부분 체결 포함)
        fills = self._fill_store.fetch_by_order(broker_order_no)
        if not fills:
            logger.info(
                "체결 없음 — analyze skip: execution_id=%s broker_order_no=%s",
                execution_id, broker_order_no,
            )
            return None

        # 종목 일치 검증
        for f in fills:
            if f.symbol != symbol:
                logger.warning(
                    "Fill 종목 불일치: execution_id=%s symbol=%s, fill_symbol=%s",
                    execution_id, symbol, f.symbol,
                )

        return self._compute_record(
            execution_id=execution_id,
            signal_id=signal_id,
            broker_order_no=broker_order_no,
            symbol=symbol,
            side=side,
            intent_price=intent_price_krw,
            intent_quantity=intent_quantity,
            intent_at_utc=intent_at_utc,
            fills=fills,
        )

    # ------------------------------------------------------------------
    # Audit log 일괄 분석
    # ------------------------------------------------------------------

    def analyze_executions_from_audit(
        self,
        audit_path: str | Path,
        *,
        since_utc: Optional[datetime] = None,
    ) -> list[SlippageRecord]:
        """
        Task 21 ExecutionAuditLog JSONL 직접 읽어서 분석.

        outcome=submitted + broker_order_no 있는 기록만 대상.
        """
        audit_path = Path(audit_path)
        if not audit_path.exists():
            logger.warning("Audit log 없음: %s", audit_path)
            return []

        records: list[SlippageRecord] = []
        with audit_path.open("r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError as e:
                    logger.warning("Audit log 라인 %d 파싱 실패: %s", line_no, e)
                    continue

                # 송신된 주문만
                if rec.get("outcome") != "submitted":
                    continue
                broker_order_no = rec.get("broker_order_no")
                if not broker_order_no:
                    # dry-run은 broker_order_no가 None — 스킵
                    continue

                # 시간 필터
                started_str = rec.get("started_at_utc")
                if since_utc is not None and started_str:
                    try:
                        started = datetime.fromisoformat(started_str)
                        if started < since_utc:
                            continue
                    except (ValueError, TypeError):
                        pass

                # 필수 필드
                aligned_price = rec.get("aligned_price")
                qty = rec.get("quantity")
                symbol = rec.get("symbol")
                side = rec.get("side")
                if not all([aligned_price, qty, symbol, side]):
                    continue

                try:
                    sr = self.analyze_execution(
                        execution_id=rec["execution_id"],
                        intent_price_krw=Decimal(str(aligned_price)),
                        intent_quantity=int(qty),
                        broker_order_no=str(broker_order_no),
                        side=str(side),
                        symbol=str(symbol),
                        intent_at_utc=datetime.fromisoformat(rec["started_at_utc"]),
                        signal_id=rec.get("signal_id"),
                    )
                except (ValueError, KeyError) as e:
                    logger.warning("분석 실패 line %d: %s", line_no, e)
                    continue
                if sr is not None:
                    records.append(sr)
        return records

    # ------------------------------------------------------------------
    # 집계 통계
    # ------------------------------------------------------------------

    @staticmethod
    def aggregate(records: Iterable[SlippageRecord]) -> dict:
        """
        다수 SlippageRecord → 집계 통계.

        Returns:
            count, avg_slippage_bps, median_slippage_bps,
            p95_slippage_bps, unfavorable_count, unfavorable_pct,
            avg_friction_bps, total_fees_krw, total_taxes_krw, etc.
        """
        records_list = list(records)
        n = len(records_list)
        if n == 0:
            return {
                "count": 0,
                "avg_slippage_bps": None,
                "median_slippage_bps": None,
                "p95_slippage_bps": None,
                "unfavorable_count": 0,
                "unfavorable_pct": None,
                "avg_friction_bps": None,
                "avg_cost_impact_bps": None,
                "total_fees_krw": "0",
                "total_taxes_krw": "0",
                "partial_fill_count": 0,
                "by_symbol": {},
            }

        slippage_floats = [float(r.slippage_bps) for r in records_list]
        friction_floats = [float(r.total_friction_bps) for r in records_list]
        cost_floats = [float(r.cost_impact_bps) for r in records_list]
        unfavorable_count = sum(1 for r in records_list if r.is_unfavorable)
        partial_count = sum(1 for r in records_list if r.is_partial)
        total_fees = sum((r.total_fees_krw for r in records_list), Decimal("0"))
        total_taxes = sum((r.total_taxes_krw for r in records_list), Decimal("0"))

        # 종목별 집계
        by_symbol: dict[str, dict] = {}
        for r in records_list:
            sym = r.symbol
            entry = by_symbol.setdefault(sym, {
                "count": 0, "sum_slippage_bps": 0.0,
                "unfavorable": 0, "fees": Decimal("0"), "taxes": Decimal("0"),
            })
            entry["count"] += 1
            entry["sum_slippage_bps"] += float(r.slippage_bps)
            if r.is_unfavorable:
                entry["unfavorable"] += 1
            entry["fees"] += r.total_fees_krw
            entry["taxes"] += r.total_taxes_krw

        by_symbol_summary = {}
        for sym, entry in by_symbol.items():
            by_symbol_summary[sym] = {
                "count": entry["count"],
                "avg_slippage_bps": str(round(entry["sum_slippage_bps"] / entry["count"], 4)),
                "unfavorable_count": entry["unfavorable"],
                "total_fees_krw": str(entry["fees"]),
                "total_taxes_krw": str(entry["taxes"]),
            }

        sorted_slip = sorted(slippage_floats)
        median_idx = n // 2
        p95_idx = max(0, min(n - 1, int(math.ceil(n * 0.95)) - 1))

        return {
            "count": n,
            "avg_slippage_bps": str(round(statistics.fmean(slippage_floats), 4)),
            "median_slippage_bps": str(round(sorted_slip[median_idx], 4)),
            "p95_slippage_bps": str(round(sorted_slip[p95_idx], 4)),
            "unfavorable_count": unfavorable_count,
            "unfavorable_pct": str(round(unfavorable_count / n, 4)),
            "avg_cost_impact_bps": str(round(statistics.fmean(cost_floats), 4)),
            "avg_friction_bps": str(round(statistics.fmean(friction_floats), 4)),
            "total_fees_krw": str(total_fees),
            "total_taxes_krw": str(total_taxes),
            "partial_fill_count": partial_count,
            "by_symbol": by_symbol_summary,
        }

    # ------------------------------------------------------------------
    # 내부 계산
    # ------------------------------------------------------------------

    def _compute_record(
        self,
        *,
        execution_id: str,
        signal_id: Optional[str],
        broker_order_no: str,
        symbol: str,
        side: str,
        intent_price: Decimal,
        intent_quantity: int,
        intent_at_utc: datetime,
        fills: list[Fill],
    ) -> SlippageRecord:
        # 합산
        total_qty = sum(f.quantity for f in fills)
        if total_qty <= 0:
            raise ValueError(f"체결 수량 0: {broker_order_no}")

        # VWAP 평균 체결가
        weighted_sum = sum(
            (f.price * Decimal(f.quantity) for f in fills),
            start=Decimal("0"),
        )
        avg_fill_price = weighted_sum / Decimal(total_qty)

        total_fees = sum((f.fee_krw for f in fills), Decimal("0"))
        total_taxes = sum((f.tax_krw for f in fills), Decimal("0"))

        # Slippage (부호: 양수 = 불리)
        if side == "buy":
            abs_slippage = avg_fill_price - intent_price
        else:  # sell
            abs_slippage = intent_price - avg_fill_price

        slippage_bps = (abs_slippage / intent_price) * Decimal("10000")
        is_unfavorable = abs_slippage > 0

        # Cost impact (항상 양수)
        gross_intent = intent_price * Decimal(intent_quantity)
        if gross_intent > 0:
            cost_impact_bps = ((total_fees + total_taxes) / gross_intent) * Decimal("10000")
        else:
            cost_impact_bps = Decimal("0")

        # Total friction = slippage + cost impact
        total_friction_bps = slippage_bps + cost_impact_bps

        # 부분 체결 여부
        is_partial = total_qty < intent_quantity or any(f.is_partial for f in fills)

        last_fill_at = max(f.filled_at_utc for f in fills)

        return SlippageRecord(
            execution_id=execution_id,
            signal_id=signal_id,
            broker_order_no=broker_order_no,
            symbol=symbol,
            side=side,
            intent_price_krw=intent_price,
            intent_quantity=intent_quantity,
            filled_quantity=total_qty,
            avg_fill_price_krw=avg_fill_price,
            fill_count=len(fills),
            abs_slippage_krw=abs_slippage,
            slippage_bps=slippage_bps,
            is_unfavorable=is_unfavorable,
            total_fees_krw=total_fees,
            total_taxes_krw=total_taxes,
            cost_impact_bps=cost_impact_bps,
            total_friction_bps=total_friction_bps,
            intent_at_utc=intent_at_utc,
            last_fill_at_utc=last_fill_at,
            is_partial=is_partial,
        )
