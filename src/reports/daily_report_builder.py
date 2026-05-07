"""
일일 리포트 빌더 (Daily Report Builder)
========================================

JCPR Trading System - jcpr-ts-v01
Task 49 v0.2

DailyReportInputs를 받아 모든 데이터 소스에서 정보를 수집하고
DailyReport (Final Output #1-12) 를 조립한다.

(Assembles DailyReport from inputs by gathering data from all sources.)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional, Sequence

from .audit_aggregator import (
    aggregate_approval_audit,
    aggregate_execution_audit,
    aggregate_risk_audit,
)
from .capacity_recommender import (
    CapacityThresholds,
    recommend_next_capacity,
)
from .daily_report import DailyReport, DailyReportInputs
from .pnl_loader import compute_pnl_snapshot


REPORT_VERSION = "0.2.0"


@dataclass
class DailyReportBuilder:
    """
    일일 리포트 조립기.

    사용 (Usage):
        builder = DailyReportBuilder()
        report = builder.build(inputs)
        # 또는 빌드 + 저장:
        paths = builder.build_and_save(inputs, "reports/2026-05-07")
    """

    capacity_thresholds: Optional[CapacityThresholds] = None

    # ─────────────────────────────────────────────
    # build (메인)
    # ─────────────────────────────────────────────

    def build(self, inputs: DailyReportInputs) -> DailyReport:
        """모든 final output 수집 → DailyReport 반환."""
        # ─── PnL 스냅샷 ─────────────────────────
        pnl = compute_pnl_snapshot(
            positions_db=inputs.positions_db,
            ohlcv_db=inputs.ohlcv_db,
            quote_db=inputs.quote_db,
            starting_capital_krw=inputs.starting_capital_krw,
            cash_krw=inputs.cash_krw,
            session_start_iso=inputs.session_start_utc.isoformat(),
            session_end_iso=inputs.session_end_utc.isoformat(),
        )

        # ─── 감사 로그 집계 ─────────────────────
        risk_stats = aggregate_risk_audit(
            inputs.risk_audit_path,
            session_start_utc=inputs.session_start_utc,
            session_end_utc=inputs.session_end_utc,
        )
        exec_stats = aggregate_execution_audit(
            inputs.execution_audit_path,
            session_start_utc=inputs.session_start_utc,
            session_end_utc=inputs.session_end_utc,
        )
        appr_stats = aggregate_approval_audit(
            inputs.approval_audit_path,
            session_start_utc=inputs.session_start_utc,
            session_end_utc=inputs.session_end_utc,
        )

        # ─── 정합성 (Reconciliation) ───────────
        recon = inputs.reconciliation_status or {
            "severity": "unknown",
            "mismatch_count": 0,
            "note": "Reconciliation 데이터 미주입 (Task 28 결과 미연결)",
        }

        # ─── 예외 (Exceptions) — Task 21 + others ─
        exceptions: list[dict[str, Any]] = []
        for em in exec_stats.error_messages:
            exceptions.append({
                "source": "execution_gateway",
                "execution_id": em.get("execution_id"),
                "symbol": em.get("symbol"),
                "stage": em.get("stage"),
                "message": em.get("message", ""),
                "timestamp_utc": em.get("started_at_utc"),
            })
        if recon.get("severity") in ("major", "minor"):
            exceptions.append({
                "source": "reconciliation",
                "message": f"정합성 {recon.get('severity')} — {recon.get('note', '')}",
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            })

        # ─── #12 자본 추천 ─────────────────────
        try:
            realized_dec = Decimal(str(pnl.get("realized_pnl_krw", "0")))
            unrealized_dec = Decimal(str(pnl.get("unrealized_pnl_krw", "0")))
        except Exception:  # noqa: BLE001
            realized_dec = Decimal(0)
            unrealized_dec = Decimal(0)

        recommendation = recommend_next_capacity(
            starting_capital_krw=inputs.starting_capital_krw,
            realized_pnl_krw=realized_dec,
            unrealized_pnl_krw=unrealized_dec,
            rejection_rate=risk_stats.rejection_rate,
            exception_count=len(exceptions),
            reconciliation_severity=str(recon.get("severity", "ok")),
            portfolio_risk_warnings=len(inputs.portfolio_risk_warnings),
            thresholds=self.capacity_thresholds,
        )

        # ─── 메타데이터 ─────────────────────────
        metadata = {
            "session_id": inputs.session_id,
            "session_date_kst": inputs.session_date_kst.isoformat(),
            "session_start_utc": inputs.session_start_utc.isoformat(),
            "session_end_utc": inputs.session_end_utc.isoformat(),
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "report_version": REPORT_VERSION,
        }

        # ─── Final Output 조립 ──────────────────
        return DailyReport(
            metadata=metadata,
            output_1_starting_capital={
                "amount_krw": str(inputs.starting_capital_krw),
            },
            output_2_ending_capital={
                "cash_krw": pnl.get("cash_krw"),
                "position_market_value_krw": pnl.get("position_market_value_krw"),
                "total_equity_krw": pnl.get("total_equity_krw"),
                "total_return_pct": pnl.get("total_return_pct"),
            },
            output_3_realized_pnl={
                "amount_krw": pnl.get("realized_pnl_krw"),
            },
            output_4_unrealized_pnl={
                "amount_krw": pnl.get("unrealized_pnl_krw"),
            },
            output_5_fees_slippage={
                "total_fees_krw": pnl.get("total_fees_krw"),
                "total_taxes_krw": pnl.get("total_taxes_krw"),
                "total_slippage_krw": pnl.get("total_slippage_krw"),
                "fill_count": pnl.get("fill_count", 0),
            },
            output_6_strategy_attribution=pnl.get("strategy_attribution", []),
            output_7_symbol_attribution={
                "positions": pnl.get("symbol_attribution", []),
                "open_position_count": pnl.get("open_position_count", 0),
            },
            output_8_rejected_orders={
                "total_evaluations": risk_stats.total_evaluations,
                "approved": risk_stats.approved,
                "rejected": risk_stats.rejected,
                "rejection_rate": risk_stats.rejection_rate,
                "by_gate": risk_stats.by_gate,
                "by_reason": risk_stats.by_reason,
                "by_symbol": risk_stats.by_symbol_rejected,
                "by_strategy": risk_stats.by_strategy_rejected,
            },
            output_9_risk_limit_usage={
                "portfolio_warning_count": len(inputs.portfolio_risk_warnings),
                "warnings": inputs.portfolio_risk_warnings,
                "approval_stats": {
                    "total_requests": appr_stats.total_requests,
                    "approved": appr_stats.approved,
                    "declined": appr_stats.declined,
                    "auto_approved": appr_stats.auto_approved,
                    "approval_rate": appr_stats.approval_rate,
                },
            },
            output_10_reconciliation_status=recon,
            output_11_exceptions=exceptions,
            output_12_next_session_capacity=recommendation.to_dict(),
        )

    # ─────────────────────────────────────────────
    # build_and_save
    # ─────────────────────────────────────────────

    def build_and_save(
        self,
        inputs: DailyReportInputs,
        output_dir: str | Path,
        *,
        formats: Sequence[str] = ("json", "md", "html"),
    ) -> dict[str, Path]:
        """
        리포트 생성 + 파일 저장.

        Args:
            inputs: DailyReportInputs
            output_dir: 출력 디렉터리 (자동 생성)
            formats: ("json", "md", "html") 중 부분 집합

        Returns:
            {format: filepath}
        """
        report = self.build(inputs)
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        date_str = inputs.session_date_kst.isoformat()
        base = f"daily_report_{date_str}"
        results: dict[str, Path] = {}

        if "json" in formats:
            p = out_dir / f"{base}.json"
            p.write_text(report.to_json(), encoding="utf-8")
            results["json"] = p

        if "md" in formats:
            p = out_dir / f"{base}.md"
            p.write_text(report.to_markdown(), encoding="utf-8")
            results["md"] = p

        if "html" in formats:
            p = out_dir / f"{base}.html"
            p.write_text(report.to_html(), encoding="utf-8")
            results["html"] = p

        return results
