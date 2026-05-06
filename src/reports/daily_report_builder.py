"""
일일 리포트 빌더 (Daily Report Builder)
=========================================

JCPR Trading System - jcpr-ts-v01
Task 49 v0.1

여러 데이터 소스에서 Final Output #1-12를 수집하여 DailyReport로 조립.
(Orchestrator that collects #1-12 from multiple sources into a DailyReport.)

원칙:
- Read-only — 모든 의존 컴포넌트는 read-only로만 사용
- Graceful degradation — 의존 부재 시 N/A 처리, 예외 발생 안 함
- 비밀 미포함
"""

from __future__ import annotations

import logging
import traceback
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional, Sequence

from .audit_aggregator import (
    aggregate_approval_audit,
    aggregate_execution_audit,
    aggregate_risk_audit,
)
from .capacity_recommender import recommend_next_capacity
from .daily_report import DailyReport, DailyReportInputs

logger = logging.getLogger(__name__)


class DailyReportBuilder:
    """
    DailyReportInputs → DailyReport 빌더.
    """

    REPORT_VERSION = "0.1"

    def build(self, inputs: DailyReportInputs) -> DailyReport:
        """모든 final output을 조립."""
        # 작업 중 예외 수집 (output #11에 포함)
        build_exceptions: list[dict[str, Any]] = []

        # ── PnL 계산 ──
        pnl_summary, pnl_data = self._safe_compute_pnl(inputs, build_exceptions)

        # ── Audit 집계 ──
        risk_stats = self._safe_aggregate(
            "risk_audit",
            lambda: aggregate_risk_audit(
                inputs.risk_audit_path,
                since_utc=inputs.session_start_utc,
                until_utc=inputs.session_end_utc,
            ) if inputs.risk_audit_path else None,
            build_exceptions,
        )
        execution_stats = self._safe_aggregate(
            "execution_audit",
            lambda: aggregate_execution_audit(
                inputs.execution_audit_path,
                since_utc=inputs.session_start_utc,
                until_utc=inputs.session_end_utc,
            ) if inputs.execution_audit_path else None,
            build_exceptions,
        )
        approval_stats = self._safe_aggregate(
            "approval_audit",
            lambda: aggregate_approval_audit(
                inputs.approval_audit_path,
                since_utc=inputs.session_start_utc,
                until_utc=inputs.session_end_utc,
            ) if inputs.approval_audit_path else None,
            build_exceptions,
        )

        # ── 슬리피지 분석 ──
        slippage_summary = self._safe_compute_slippage(inputs, build_exceptions)

        # ── 포트폴리오 리스크 ──
        portfolio_risk = self._safe_compute_portfolio_risk(
            inputs, pnl_data, build_exceptions,
        )

        # ── 정합성 점검 ──
        recon_status = self._safe_compute_reconciliation(inputs, build_exceptions)

        # ── 예외 수집 ──
        all_exceptions = self._collect_all_exceptions(
            build_exceptions=build_exceptions,
            execution_stats=execution_stats,
            recon_status=recon_status,
        )

        # ── 자본 추천 ──
        capacity = self._compute_capacity_recommendation(
            inputs=inputs,
            pnl_summary=pnl_summary,
            risk_stats=risk_stats,
            recon_status=recon_status,
            portfolio_risk=portfolio_risk,
            exceptions_count=len(all_exceptions),
        )

        # ── DailyReport 조립 ──
        return self._assemble_report(
            inputs=inputs,
            pnl_summary=pnl_summary,
            pnl_data=pnl_data,
            risk_stats=risk_stats,
            execution_stats=execution_stats,
            approval_stats=approval_stats,
            slippage_summary=slippage_summary,
            portfolio_risk=portfolio_risk,
            recon_status=recon_status,
            all_exceptions=all_exceptions,
            capacity=capacity,
        )

    # ------------------------------------------------------------------
    # build_and_save
    # ------------------------------------------------------------------

    def build_and_save(
        self,
        inputs: DailyReportInputs,
        output_dir: str | Path,
        *,
        formats: Sequence[str] = ("json", "md", "html"),
        filename_prefix: str = "daily_report",
    ) -> dict[str, Path]:
        """build + 파일 저장. Returns: {format: filepath}"""
        report = self.build(inputs)
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        date_str = inputs.session_date_kst.isoformat()
        base = f"{filename_prefix}_{date_str}"
        result: dict[str, Path] = {}

        if "json" in formats:
            result["json"] = report.save_json(out_dir / f"{base}.json")
        if "md" in formats or "markdown" in formats:
            result["md"] = report.save_markdown(out_dir / f"{base}.md")
        if "html" in formats:
            result["html"] = report.save_html(out_dir / f"{base}.html")

        return result

    # ------------------------------------------------------------------
    # PnL 계산
    # ------------------------------------------------------------------

    def _safe_compute_pnl(
        self,
        inputs: DailyReportInputs,
        exceptions: list[dict[str, Any]],
    ) -> tuple[Optional[dict], Optional[Any]]:
        if inputs.pnl_engine is None:
            return None, None
        try:
            port_pnl = inputs.pnl_engine.compute_portfolio_pnl(
                starting_capital_krw=inputs.starting_capital_krw,
                cash_krw=inputs.cash_krw,
                as_of_utc=inputs.session_end_utc,
            )
            return port_pnl.to_summary_dict(), port_pnl
        except Exception as e:  # noqa: BLE001
            logger.exception("PnL 계산 실패")
            exceptions.append({
                "source": "pnl_engine",
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "message": f"{type(e).__name__}: {e}",
                "traceback": traceback.format_exc(),
            })
            return None, None

    # ------------------------------------------------------------------
    # 슬리피지 계산
    # ------------------------------------------------------------------

    def _safe_compute_slippage(
        self,
        inputs: DailyReportInputs,
        exceptions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if inputs.slippage_analyzer is None or inputs.execution_audit_path is None:
            return {"count": 0, "available": False}
        try:
            records = inputs.slippage_analyzer.analyze_executions_from_audit(
                inputs.execution_audit_path,
                since_utc=inputs.session_start_utc,
            )
            agg = inputs.slippage_analyzer.aggregate(records)
            agg["available"] = True
            return agg
        except Exception as e:  # noqa: BLE001
            logger.exception("슬리피지 분석 실패")
            exceptions.append({
                "source": "slippage_analyzer",
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "message": f"{type(e).__name__}: {e}",
            })
            return {"count": 0, "available": False, "error": str(e)}

    # ------------------------------------------------------------------
    # 포트폴리오 리스크
    # ------------------------------------------------------------------

    def _safe_compute_portfolio_risk(
        self,
        inputs: DailyReportInputs,
        pnl_data: Optional[Any],
        exceptions: list[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        if inputs.portfolio_risk_analyzer is None or pnl_data is None:
            return None
        try:
            # PortfolioPnL.positions 의 SymbolPnL 정보를 dict로 변환 — RiskContext.open_positions 형식
            positions_for_risk: dict[str, dict[str, Any]] = {}
            for sym, sp in pnl_data.positions.items():
                if sp.quantity > 0:
                    positions_for_risk[sym] = {
                        "market_value_krw": str(sp.market_value_krw),
                    }

            snap = inputs.portfolio_risk_analyzer.analyze(
                positions=positions_for_risk,
                equity_krw=pnl_data.ending_capital_krw,
                as_of_utc=inputs.session_end_utc,
            )
            return snap.to_dict()
        except Exception as e:  # noqa: BLE001
            logger.exception("포트폴리오 리스크 분석 실패")
            exceptions.append({
                "source": "portfolio_risk_analyzer",
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "message": f"{type(e).__name__}: {e}",
            })
            return None

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------

    def _safe_compute_reconciliation(
        self,
        inputs: DailyReportInputs,
        exceptions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if inputs.reconciler is None:
            return {
                "performed": False,
                "reason": "Reconciler 미주입 (broker connection 없음)",
            }
        try:
            report = inputs.reconciler.reconcile()
            d = report.to_dict()
            d["performed"] = True
            return d
        except Exception as e:  # noqa: BLE001
            logger.exception("Reconciliation 실패")
            exceptions.append({
                "source": "reconciler",
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "message": f"{type(e).__name__}: {e}",
            })
            return {
                "performed": False,
                "reason": f"점검 실패: {type(e).__name__}: {e}",
            }

    # ------------------------------------------------------------------
    # Audit 집계 (예외 처리)
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_aggregate(
        source_name: str,
        callable_,
        exceptions: list[dict[str, Any]],
    ):
        try:
            return callable_()
        except Exception as e:  # noqa: BLE001
            logger.exception("audit 집계 실패: %s", source_name)
            exceptions.append({
                "source": source_name,
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "message": f"{type(e).__name__}: {e}",
            })
            return None

    # ------------------------------------------------------------------
    # 모든 예외 수집
    # ------------------------------------------------------------------

    @staticmethod
    def _collect_all_exceptions(
        *,
        build_exceptions: list[dict[str, Any]],
        execution_stats: Any,
        recon_status: dict[str, Any],
    ) -> list[dict[str, Any]]:
        all_exc = list(build_exceptions)

        # Task 21 outcome="error" 기록
        if execution_stats is not None:
            for er in getattr(execution_stats, "error_records", []) or []:
                all_exc.append({
                    "source": "execution_audit",
                    "timestamp_utc": er.get("started_at_utc"),
                    "message": (
                        f"execution_id={er.get('execution_id')}, "
                        f"symbol={er.get('symbol')}, "
                        f"error={er.get('error')}, "
                        f"stage={er.get('stage')}"
                    ),
                })

        # Reconciliation major
        if recon_status.get("performed") and recon_status.get("severity") == "major":
            all_exc.append({
                "source": "reconciliation",
                "timestamp_utc": recon_status.get("captured_at_utc"),
                "message": (
                    f"Reconciliation severity=major, "
                    f"mismatches={recon_status.get('mismatch_count')}"
                ),
            })

        return all_exc

    # ------------------------------------------------------------------
    # Capacity Recommendation
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_capacity_recommendation(
        *,
        inputs: DailyReportInputs,
        pnl_summary: Optional[dict[str, Any]],
        risk_stats: Any,
        recon_status: dict[str, Any],
        portfolio_risk: Optional[dict[str, Any]],
        exceptions_count: int,
    ) -> dict[str, Any]:
        realized = Decimal("0")
        if pnl_summary:
            try:
                realized = Decimal(str(pnl_summary.get("realized_pnl_krw", "0")))
            except Exception:  # noqa: BLE001
                realized = Decimal("0")

        rejected_count = 0
        rejection_rate = 0.0
        if risk_stats is not None:
            rejected_count = risk_stats.reject_count
            rejection_rate = risk_stats.rejection_rate

        recon_severity = "ok"
        if recon_status.get("performed"):
            recon_severity = recon_status.get("severity", "ok")
        else:
            recon_severity = "unknown"

        portfolio_warnings = 0
        if portfolio_risk:
            portfolio_warnings = len(portfolio_risk.get("warnings", []))

        rec = recommend_next_capacity(
            starting_capital_krw=inputs.starting_capital_krw,
            realized_pnl_krw=realized,
            rejected_orders_count=rejected_count,
            rejection_rate=rejection_rate,
            exceptions_count=exceptions_count,
            reconciliation_severity=recon_severity,
            portfolio_risk_warnings=portfolio_warnings,
        )
        return rec.to_dict()

    # ------------------------------------------------------------------
    # 조립
    # ------------------------------------------------------------------

    def _assemble_report(
        self,
        *,
        inputs: DailyReportInputs,
        pnl_summary: Optional[dict[str, Any]],
        pnl_data: Optional[Any],
        risk_stats: Any,
        execution_stats: Any,
        approval_stats: Any,
        slippage_summary: dict[str, Any],
        portfolio_risk: Optional[dict[str, Any]],
        recon_status: dict[str, Any],
        all_exceptions: list[dict[str, Any]],
        capacity: dict[str, Any],
    ) -> DailyReport:
        # Metadata
        metadata = {
            "session_id": inputs.session_id,
            "session_date_kst": inputs.session_date_kst.isoformat(),
            "session_start_utc": inputs.session_start_utc.isoformat(),
            "session_end_utc": inputs.session_end_utc.isoformat(),
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "report_version": self.REPORT_VERSION,
            "system": "jcpr-ts-v01",
        }

        # #1 시작 자본
        out1 = {
            "starting_capital_krw": str(inputs.starting_capital_krw),
            "starting_cash_krw": str(inputs.cash_krw),
        }

        # #2 종료 자본
        if pnl_summary:
            out2 = {
                "ending_capital_krw": pnl_summary.get("ending_capital_krw"),
                "cash_krw": pnl_summary.get("cash_krw"),
                "total_market_value_krw": pnl_summary.get("total_market_value_krw"),
                "return_pct": pnl_summary.get("return_pct"),
            }
        else:
            out2 = {
                "ending_capital_krw": "N/A",
                "cash_krw": str(inputs.cash_krw),
                "total_market_value_krw": "N/A",
                "return_pct": None,
                "note": "PnLEngine 미제공 — 종료 자본 계산 불가",
            }

        # #3 실현 P&L
        out3 = {
            "total_realized_pnl_krw": (
                pnl_summary.get("realized_pnl_krw") if pnl_summary else "N/A"
            ),
        }

        # #4 미실현 P&L
        out4 = {
            "total_unrealized_pnl_krw": (
                pnl_summary.get("unrealized_pnl_krw") if pnl_summary else "N/A"
            ),
            "stale_symbols": (
                pnl_summary.get("stale_symbols", []) if pnl_summary else []
            ),
        }

        # #5 수수료 + 슬리피지
        out5 = {
            "total_fees_krw": (
                pnl_summary.get("fees_krw") if pnl_summary else "N/A"
            ),
            "total_taxes_krw": (
                pnl_summary.get("taxes_krw") if pnl_summary else "N/A"
            ),
            "slippage": slippage_summary,
        }

        # #6 전략 attribution
        out6 = (
            pnl_summary.get("strategy_attribution", []) if pnl_summary else []
        )

        # #7 종목 attribution
        out7 = (
            pnl_summary.get("symbol_attribution", {}) if pnl_summary else {}
        )

        # #8 거부된 주문
        if risk_stats is not None:
            out8 = risk_stats.to_dict()
        else:
            out8 = {
                "total": 0, "pass_count": 0, "reject_count": 0,
                "rejection_rate": 0.0,
                "by_gate_reject": {}, "by_symbol_reject": {}, "by_strategy_reject": {},
                "note": "Task 19 risk audit 데이터 없음",
            }

        # #9 리스크 한도 사용량
        gate_pass_rates: dict[str, float] = {}
        if risk_stats is not None and risk_stats.total > 0:
            # 게이트별 통과율은 risk audit 만으로 계산 어려움 (게이트별 평가 횟수 필요)
            # 간단히 전체 통과율만 표시
            gate_pass_rates["overall_pass_rate"] = (
                risk_stats.pass_count / risk_stats.total
            )
        out9 = {
            "portfolio": portfolio_risk if portfolio_risk else {},
            "gate_pass_rates": gate_pass_rates,
        }

        # #10 정합성
        out10 = recon_status

        # #11 예외
        out11 = all_exceptions

        # #12 자본 추천
        out12 = capacity

        return DailyReport(
            metadata=metadata,
            output_1_starting_capital=out1,
            output_2_ending_capital=out2,
            output_3_realized_pnl=out3,
            output_4_unrealized_pnl=out4,
            output_5_fees_slippage=out5,
            output_6_strategy_attribution=out6,
            output_7_symbol_attribution=out7,
            output_8_rejected_orders=out8,
            output_9_risk_limit_usage=out9,
            output_10_reconciliation_status=out10,
            output_11_exceptions=out11,
            output_12_next_session_capacity=out12,
        )
