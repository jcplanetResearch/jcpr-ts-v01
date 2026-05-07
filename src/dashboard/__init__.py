"""
대시보드 패키지 (Dashboard Package)
====================================

JCPR Trading System - jcpr-ts-v01
Task 48 v0.1.1

Streamlit 기반 실시간 모니터링 대시보드.
(Streamlit-based real-time monitoring dashboard.)

실행 (Run):
    streamlit run src/dashboard/app.py --server.address=127.0.0.1
    또는 (or):
    bash scripts/run_dashboard.sh

설계 원칙 (Design Principles):
    - 로컬 전용 (localhost only) — 외부 노출 금지
    - 시크릿 절대 표시 안 함 (never display secrets)
    - 읽기 전용 (read-only) — 거래 실행 기능 없음
    - 캐싱으로 DB 부하 최소화 (cache to reduce DB load)
"""

from .data_loader import (
    DashboardDataSource,
    load_audit_summary,
    load_fills,
    load_market_status,
    load_pnl_snapshot,
    load_positions,
    load_rejection_summary,
)

__all__ = [
    "DashboardDataSource",
    "load_audit_summary",
    "load_fills",
    "load_market_status",
    "load_pnl_snapshot",
    "load_positions",
    "load_rejection_summary",
]

__version__ = "0.1.1"
