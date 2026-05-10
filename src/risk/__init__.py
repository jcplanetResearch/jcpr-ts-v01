"""
리스크 패키지 (Risk Package)
=============================

JCPR Trading System - jcpr-ts-v01

Task 47 v0.2 — Portfolio Risk Analyzer (독립 설계, read-only)

이전 Task 19 게이트들과 통합 시 본 모듈은 read-only 분석으로 사용.
(Read-only analyzer; will integrate with Task 19 gates as wrappers in future.)
"""

from .portfolio_risk import (
    ETF_SECTOR,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    SEVERITY_WARNING,
    UNKNOWN_SECTOR,
    PortfolioRiskAnalyzer,
    PortfolioRiskConfig,
    PortfolioRiskSnapshot,
    ProjectedImpact,
    quick_analyze,
)
# 기존 import 블록 끝에 추가:
from .capacity_advisor import (
    ALGORITHM_ID, DEFAULT_CONSECUTIVE_LOSS_THRESHOLD, DEFAULT_FLAT_THRESHOLD_RATIO,
    CapacityAdvisor, CapacityAdvisorError, CapacityRecommendation,
    HistoryStats, InvalidLadderError, SessionSignals,
)

__all__ = [
    # 분석기
    "PortfolioRiskAnalyzer",
    "PortfolioRiskConfig",
    "PortfolioRiskSnapshot",
    "ProjectedImpact",
    "quick_analyze",
    # 상수
    "SEVERITY_OK",
    "SEVERITY_WARNING",
    "SEVERITY_CRITICAL",
    "UNKNOWN_SECTOR",
    "ETF_SECTOR",
    # 추가
    "CapacityAdvisor", "CapacityRecommendation", "SessionSignals",
    "HistoryStats", "InvalidLadderError", "CapacityAdvisorError",
    "ALGORITHM_ID", "DEFAULT_FLAT_THRESHOLD_RATIO",
    "DEFAULT_CONSECUTIVE_LOSS_THRESHOLD",
]

__version__ = "0.2.0"
