"""
리포트 패키지 (Reports Package)
================================

JCPR Trading System - jcpr-ts-v01
Task 49 v0.2 — Daily Report Generator (Final Output #1-12)

일일 거래 종합 리포트를 자동 생성한다.
(Auto-generates daily trading summary report.)

3개 포맷 동시 산출 (Three formats produced):
    - JSON  : 기계 처리용 (machine-readable)
    - Markdown : 사람 읽기 + GitHub 공유용
    - HTML  : 브라우저 보기 + PDF 인쇄용

사용 (Usage):
    from datetime import date, datetime, timezone
    from decimal import Decimal
    from pathlib import Path

    from src.reports import DailyReportBuilder, DailyReportInputs

    inputs = DailyReportInputs(
        session_id="session-2026-05-07",
        session_date_kst=date(2026, 5, 7),
        starting_capital_krw=Decimal("10000000"),
        cash_krw=Decimal("9500000"),
        session_start_utc=datetime(2026, 5, 7, 0, tzinfo=timezone.utc),
        session_end_utc=datetime(2026, 5, 7, 6, 30, tzinfo=timezone.utc),
        positions_db="data/db/positions.db",
        ohlcv_db="data/db/ohlcv.db",
        risk_audit_path="data/audit/risk_decisions.jsonl",
        execution_audit_path="data/audit/executions.jsonl",
    )
    builder = DailyReportBuilder()
    paths = builder.build_and_save(inputs, "reports/2026-05-07")
"""

from .audit_aggregator import (
    ApprovalAuditStats,
    ExecutionAuditStats,
    RiskAuditStats,
    aggregate_approval_audit,
    aggregate_execution_audit,
    aggregate_risk_audit,
)
from .capacity_recommender import (
    CapacityRecommendation,
    CapacityThresholds,
    recommend_next_capacity,
)
from .daily_report import DailyReport, DailyReportInputs
from .daily_report_builder import DailyReportBuilder
from .pnl_loader import compute_pnl_snapshot

__all__ = [
    # 데이터 모델
    "DailyReport",
    "DailyReportInputs",
    "ApprovalAuditStats",
    "ExecutionAuditStats",
    "RiskAuditStats",
    "CapacityRecommendation",
    "CapacityThresholds",
    # 빌더 + 헬퍼
    "DailyReportBuilder",
    "aggregate_approval_audit",
    "aggregate_execution_audit",
    "aggregate_risk_audit",
    "recommend_next_capacity",
    "compute_pnl_snapshot",
]

__version__ = "0.2.0"
