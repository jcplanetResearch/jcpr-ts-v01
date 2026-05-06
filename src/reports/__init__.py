"""리포트 패키지 (Reports Package).

Task 49 v0.1 — Daily Report Generator (Final Output #1-12)
Task 20 v0.1 — Risk Rejection Analysis (detailed per-gate diagnostics)
"""

# Task 49
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

# Task 20
from .rejection_analyzer import RejectionAnalyzer
from .rejection_diagnostics import (
    DEFAULT_THRESHOLDS,
    DiagnosticFinding,
    SEVERITY_CRITICAL,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    diagnose,
)
from .rejection_report import GateRejectionAnalysis, RejectionReport

__all__ = [
    # Task 49
    "DailyReport",
    "DailyReportInputs",
    "ApprovalAuditStats",
    "ExecutionAuditStats",
    "RiskAuditStats",
    "CapacityRecommendation",
    "DailyReportBuilder",
    "aggregate_approval_audit",
    "aggregate_execution_audit",
    "aggregate_risk_audit",
    "recommend_next_capacity",
    # Task 20
    "RejectionAnalyzer",
    "RejectionReport",
    "GateRejectionAnalysis",
    "DiagnosticFinding",
    "DEFAULT_THRESHOLDS",
    "SEVERITY_CRITICAL",
    "SEVERITY_WARNING",
    "SEVERITY_INFO",
    "diagnose",
]
