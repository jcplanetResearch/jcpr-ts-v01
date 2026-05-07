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
]

__version__ = "0.2.0"
