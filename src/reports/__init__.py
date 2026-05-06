"""리포트 패키지 (Reports Package) — Task 49 v0.1.

일일 리포트 자동 생성 — Final Output #1-12 산출.
(Daily report generator — produces final output items #1-12.)
"""

from .audit_aggregator import (
    ApprovalAuditStats,
    ExecutionAuditStats,
    RiskAuditStats,
    aggregate_approval_audit,
    aggregate_execution_audit,
    aggregate_risk_audit,
)
from .capacity_recommender import CapacityRecommendation, recommend_next_capacity
from .daily_report import DailyReport, DailyReportInputs
from .daily_report_builder import DailyReportBuilder

__all__ = [
    # 데이터 모델
    "DailyReport",
    "DailyReportInputs",
    "ApprovalAuditStats",
    "ExecutionAuditStats",
    "RiskAuditStats",
    "CapacityRecommendation",
    # 빌더 + 헬퍼
    "DailyReportBuilder",
    "aggregate_approval_audit",
    "aggregate_execution_audit",
    "aggregate_risk_audit",
    "recommend_next_capacity",
]
