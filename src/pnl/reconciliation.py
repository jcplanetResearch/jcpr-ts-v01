"""
정합성 점검 (Reconciliation)
==============================

JCPR Trading System - jcpr-ts-v01
Task 28 v0.1

브로커(KIS) 잔고/포지션 vs 내부 원장(Task 25) 비교.
(Compare broker positions vs internal ledger.)

원칙 (Principles):
- Read-only — 자동 복구 안 함 (운영자 수동 처리)
- 평균가 허용 오차: ±1원 OR ±1bp 이내
- 모든 datetime UTC tz-aware
- 비밀 미포함
- v0.1: 단일 계좌 비교
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class MismatchType(str, Enum):
    BROKER_ONLY = "broker_only"      # 브로커에만 있음
    LEDGER_ONLY = "ledger_only"      # 내부에만 있음
    QUANTITY = "quantity"             # 수량 불일치
    AVG_PRICE = "avg_price"           # 평균가 불일치 (허용 오차 초과)


@dataclass(frozen=True)
class PositionMismatch:
    """단일 종목 불일치."""
    symbol: str
    type: MismatchType
    broker_quantity: Optional[int]
    ledger_quantity: Optional[int]
    broker_avg_price_krw: Optional[Decimal]
    ledger_avg_price_krw: Optional[Decimal]
    diff_quantity: int                       # broker - ledger (없는 쪽 = 0 처리)
    diff_avg_price_krw: Optional[Decimal]    # broker - ledger
    detail: str

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "type": self.type.value,
            "broker_quantity": self.broker_quantity,
            "ledger_quantity": self.ledger_quantity,
            "broker_avg_price_krw": (
                str(self.broker_avg_price_krw) if self.broker_avg_price_krw is not None else None
            ),
            "ledger_avg_price_krw": (
                str(self.ledger_avg_price_krw) if self.ledger_avg_price_krw is not None else None
            ),
            "diff_quantity": self.diff_quantity,
            "diff_avg_price_krw": (
                str(self.diff_avg_price_krw) if self.diff_avg_price_krw is not None else None
            ),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ReconciliationReport:
    captured_at_utc: datetime
    broker_position_count: int
    ledger_position_count: int
    matches: list[str] = field(default_factory=list)
    mismatches: list[PositionMismatch] = field(default_factory=list)
    broker_cash_krw: Decimal = Decimal("0")
    broker_total_evaluation_krw: Decimal = Decimal("0")
    avg_price_tolerance_krw: Decimal = Decimal("1")
    avg_price_tolerance_bps: Decimal = Decimal("1")

    def all_matched(self) -> bool:
        return len(self.mismatches) == 0

    def severity(self) -> str:
        """ok / minor / major"""
        if not self.mismatches:
            return "ok"
        major_types = {
            MismatchType.BROKER_ONLY,
            MismatchType.LEDGER_ONLY,
            MismatchType.QUANTITY,
        }
        if any(m.type in major_types for m in self.mismatches):
            return "major"
        return "minor"

    def by_type(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for m in self.mismatches:
            counts[m.type.value] = counts.get(m.type.value, 0) + 1
        return counts

    def to_dict(self) -> dict:
        return {
            "captured_at_utc": self.captured_at_utc.isoformat(),
            "severity": self.severity(),
            "all_matched": self.all_matched(),
            "broker_position_count": self.broker_position_count,
            "ledger_position_count": self.ledger_position_count,
            "match_count": len(self.matches),
            "mismatch_count": len(self.mismatches),
            "matches": list(self.matches),
            "mismatches": [m.to_dict() for m in self.mismatches],
            "by_type": self.by_type(),
            "broker_cash_krw": str(self.broker_cash_krw),
            "broker_total_evaluation_krw": str(self.broker_total_evaluation_krw),
            "tolerance": {
                "avg_price_krw": str(self.avg_price_tolerance_krw),
                "avg_price_bps": str(self.avg_price_tolerance_bps),
            },
        }


# ─────────────────────────────────────────────────
# Reconciler
# ─────────────────────────────────────────────────

class Reconciler:
    """
    브로커 + 내부 원장 정합성 점검기.

    Args:
        kis_account: Task 8 KISAccount (fetch_account_snapshot)
        ledger:      Task 25 PositionLedger (get_all)
        avg_price_tolerance_krw: 평균가 허용 절대 오차 (KRW)
        avg_price_tolerance_bps: 평균가 허용 상대 오차 (bps)
    """

    def __init__(
        self,
        kis_account,
        ledger,
        *,
        avg_price_tolerance_krw: Decimal = Decimal("1"),
        avg_price_tolerance_bps: Decimal = Decimal("1"),
    ):
        self._account = kis_account
        self._ledger = ledger
        if avg_price_tolerance_krw < 0:
            raise ValueError("avg_price_tolerance_krw 음수 불가")
        if avg_price_tolerance_bps < 0:
            raise ValueError("avg_price_tolerance_bps 음수 불가")
        self._tol_krw = avg_price_tolerance_krw
        self._tol_bps = avg_price_tolerance_bps

    # ------------------------------------------------------------------
    # 메인
    # ------------------------------------------------------------------

    def reconcile(self) -> ReconciliationReport:
        """
        브로커 + ledger 비교 → 보고서.
        Read-only (변경 없음).
        """
        # 브로커 스냅샷 조회 (실 KIS 호출)
        snap = self._account.fetch_account_snapshot()
        broker_positions = snap.positions  # dict[str, PositionInfo]

        # 내부 원장 (활성 포지션만)
        ledger_positions = self._ledger.get_all(only_active=True)

        broker_symbols = set(broker_positions.keys())
        ledger_symbols = set(ledger_positions.keys())

        matches: list[str] = []
        mismatches: list[PositionMismatch] = []

        # 1) 브로커에만 있는 종목
        for sym in sorted(broker_symbols - ledger_symbols):
            bp = broker_positions[sym]
            mismatches.append(PositionMismatch(
                symbol=sym,
                type=MismatchType.BROKER_ONLY,
                broker_quantity=bp.quantity,
                ledger_quantity=None,
                broker_avg_price_krw=bp.avg_price_krw,
                ledger_avg_price_krw=None,
                diff_quantity=bp.quantity,  # ledger 0 처리
                diff_avg_price_krw=None,
                detail=(
                    f"브로커에만 보유 (broker only): qty={bp.quantity}, "
                    f"avg={bp.avg_price_krw}"
                ),
            ))

        # 2) 내부에만 있는 종목
        for sym in sorted(ledger_symbols - broker_symbols):
            lp = ledger_positions[sym]
            mismatches.append(PositionMismatch(
                symbol=sym,
                type=MismatchType.LEDGER_ONLY,
                broker_quantity=None,
                ledger_quantity=lp.quantity,
                broker_avg_price_krw=None,
                ledger_avg_price_krw=lp.avg_cost_krw,
                diff_quantity=-lp.quantity,
                diff_avg_price_krw=None,
                detail=(
                    f"내부 원장에만 보유 (ledger only): qty={lp.quantity}, "
                    f"avg={lp.avg_cost_krw}"
                ),
            ))

        # 3) 양쪽에 있는 종목 — 수량/평균가 비교
        for sym in sorted(broker_symbols & ledger_symbols):
            bp = broker_positions[sym]
            lp = ledger_positions[sym]

            qty_diff = bp.quantity - lp.quantity
            price_diff = bp.avg_price_krw - lp.avg_cost_krw

            # 수량 불일치
            if qty_diff != 0:
                mismatches.append(PositionMismatch(
                    symbol=sym,
                    type=MismatchType.QUANTITY,
                    broker_quantity=bp.quantity,
                    ledger_quantity=lp.quantity,
                    broker_avg_price_krw=bp.avg_price_krw,
                    ledger_avg_price_krw=lp.avg_cost_krw,
                    diff_quantity=qty_diff,
                    diff_avg_price_krw=price_diff,
                    detail=(
                        f"수량 불일치: broker={bp.quantity}, ledger={lp.quantity}, "
                        f"diff={qty_diff:+d}"
                    ),
                ))
                continue

            # 수량 일치 → 평균가 비교
            if not self._avg_price_within_tolerance(
                bp.avg_price_krw, lp.avg_cost_krw,
            ):
                # 차이 bps 계산 (디스플레이용)
                diff_bps = (
                    (price_diff / lp.avg_cost_krw) * Decimal("10000")
                    if lp.avg_cost_krw > 0 else Decimal("0")
                )
                mismatches.append(PositionMismatch(
                    symbol=sym,
                    type=MismatchType.AVG_PRICE,
                    broker_quantity=bp.quantity,
                    ledger_quantity=lp.quantity,
                    broker_avg_price_krw=bp.avg_price_krw,
                    ledger_avg_price_krw=lp.avg_cost_krw,
                    diff_quantity=0,
                    diff_avg_price_krw=price_diff,
                    detail=(
                        f"평균가 불일치 (허용 오차 초과): "
                        f"broker={bp.avg_price_krw}, ledger={lp.avg_cost_krw}, "
                        f"diff={price_diff:+}, {diff_bps:+.2f}bps"
                    ),
                ))
                continue

            # 모두 일치
            matches.append(sym)

        report = ReconciliationReport(
            captured_at_utc=datetime.now(timezone.utc),
            broker_position_count=len(broker_positions),
            ledger_position_count=len(ledger_positions),
            matches=matches,
            mismatches=mismatches,
            broker_cash_krw=snap.cash_krw,
            broker_total_evaluation_krw=snap.total_evaluation_krw,
            avg_price_tolerance_krw=self._tol_krw,
            avg_price_tolerance_bps=self._tol_bps,
        )

        logger.info(
            "Reconciliation 완료: severity=%s, matches=%d, mismatches=%d",
            report.severity(), len(matches), len(mismatches),
        )
        return report

    # ------------------------------------------------------------------
    # JSONL audit
    # ------------------------------------------------------------------

    def report_to_jsonl(
        self,
        report: ReconciliationReport,
        path: str | Path,
    ) -> None:
        """Reconciliation 결과를 JSONL audit log에 append."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(report.to_dict(), ensure_ascii=False) + "\n")
        except OSError as e:
            logger.error("Reconciliation audit 기록 실패: %s", e)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _avg_price_within_tolerance(
        self, broker_avg: Decimal, ledger_avg: Decimal,
    ) -> bool:
        """평균가 허용 오차 내인가 — 절대 OR 상대."""
        diff = abs(broker_avg - ledger_avg)
        # 절대 오차
        if diff <= self._tol_krw:
            return True
        # 상대 오차 (bps)
        if ledger_avg > 0:
            bps = (diff / ledger_avg) * Decimal("10000")
            if bps <= self._tol_bps:
                return True
        return False
