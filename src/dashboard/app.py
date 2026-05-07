"""
Streamlit 대시보드 진입점 (Dashboard Entry Point)
==================================================

JCPR Trading System - jcpr-ts-v01
Task 48 v0.1.1

실행 (Run):
    streamlit run src/dashboard/app.py --server.address=127.0.0.1
    또는:
    bash scripts/run_dashboard.sh

보안 (Security):
    - 로컬 전용 (localhost only) — --server.address=127.0.0.1
    - DB 경로는 사이드바 입력 또는 환경변수에서 (no hardcoding)
    - 자격증명은 절대 화면에 표시 안 함 (no credentials displayed)
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import streamlit as st

# 패키지 import 경로 보정 (streamlit run으로 직접 실행될 때)
import sys
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent  # src/dashboard/app.py → repo root
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.dashboard.data_loader import DashboardDataSource  # noqa: E402
from src.dashboard.views import (  # noqa: E402
    render_fills_view,
    render_overview_view,
    render_positions_view,
    render_risk_view,
    render_system_view,
)


# ─────────────────────────────────────────────────
# 페이지 설정 (Page Config)
# ─────────────────────────────────────────────────

st.set_page_config(
    page_title="JCPR Trading Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ─────────────────────────────────────────────────
# 사이드바 (Sidebar)
# ─────────────────────────────────────────────────

def _render_sidebar() -> tuple[DashboardDataSource, float, float, str | None]:
    """사이드바 렌더링 + 설정 반환."""
    st.sidebar.title("⚙️ 설정 (Settings)")
    st.sidebar.caption("JCPR Trading System v0.1.1")

    # 데이터 소스 경로
    st.sidebar.subheader("📁 데이터 소스 (Data Sources)")

    positions_db = st.sidebar.text_input(
        "Positions DB",
        value=os.environ.get("JCPR_POSITIONS_DB", ""),
        help="포지션·체결·주문 DB 경로 (Task 23-25)",
    )
    ohlcv_db = st.sidebar.text_input(
        "OHLCV DB",
        value=os.environ.get("JCPR_OHLCV_DB", ""),
        help="OHLCV 가격 DB 경로 (Task 12)",
    )
    quote_db = st.sidebar.text_input(
        "Quote DB",
        value=os.environ.get("JCPR_QUOTE_DB", ""),
        help="실시간 호가 DB 경로 (Task 13)",
    )
    risk_audit_path = st.sidebar.text_input(
        "Risk Audit Log (.jsonl)",
        value=os.environ.get("JCPR_RISK_AUDIT", ""),
        help="리스크 게이트 감사 로그 (Task 19)",
    )
    execution_audit_path = st.sidebar.text_input(
        "Execution Audit Log (.jsonl)",
        value=os.environ.get("JCPR_EXEC_AUDIT", ""),
        help="실행 게이트웨이 감사 로그 (Task 21)",
    )
    rejection_report_path = st.sidebar.text_input(
        "Rejection Report (선택)",
        value=os.environ.get("JCPR_REJECTION_REPORT", ""),
        help="Task 20 거부 분석 리포트",
    )
    kill_switch_file = st.sidebar.text_input(
        "Kill Switch File",
        value=os.environ.get("JCPR_KILL_SWITCH", "runtime/KILL_SWITCH_ON"),
        help="Task 31 kill switch 감시 파일",
    )
    capacity_config = st.sidebar.text_input(
        "Capacity Config",
        value=os.environ.get("JCPR_CAPACITY_CONFIG", "configs/capacity.yaml"),
        help="capacity.yaml 경로 (Task 5)",
    )

    st.sidebar.divider()

    # 세션 정보
    st.sidebar.subheader("💰 세션 정보 (Session)")
    starting_capital_krw = st.sidebar.number_input(
        "시작 자본 (Starting Capital, KRW)",
        min_value=0.0,
        value=float(os.environ.get("JCPR_STARTING_CAPITAL", "10000000")),
        step=1_000_000.0,
        format="%.0f",
    )
    cash_krw = st.sidebar.number_input(
        "현금 잔고 (Cash Balance, KRW)",
        min_value=0.0,
        value=float(os.environ.get("JCPR_CASH", "10000000")),
        step=100_000.0,
        format="%.0f",
        help="브로커 API에서 자동 갱신될 예정",
    )

    st.sidebar.divider()

    # 시간 범위
    st.sidebar.subheader("⏱️ 시간 범위 (Time Range)")
    range_choice = st.sidebar.selectbox(
        "조회 범위 (Lookback)",
        options=["오늘 (Today)", "최근 24시간 (24h)", "최근 7일 (7 days)", "전체 (All)"],
        index=0,
    )
    now_utc = datetime.now(timezone.utc)
    if range_choice.startswith("오늘"):
        # 오늘 KST 0시 → UTC
        kst_now = now_utc.astimezone(timezone(timedelta(hours=9)))
        kst_midnight = kst_now.replace(hour=0, minute=0, second=0, microsecond=0)
        since_utc = kst_midnight.astimezone(timezone.utc)
        since_iso = since_utc.isoformat()
    elif range_choice.startswith("최근 24"):
        since_iso = (now_utc - timedelta(hours=24)).isoformat()
    elif range_choice.startswith("최근 7"):
        since_iso = (now_utc - timedelta(days=7)).isoformat()
    else:
        since_iso = None

    if since_iso:
        st.sidebar.caption(f"이후 (since): `{since_iso}`")

    st.sidebar.divider()

    # 새로고침
    if st.sidebar.button("🔄 새로고침 (Refresh)", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    st.sidebar.caption("⚠️ 자격증명·시크릿은 절대 입력하지 마세요.\n(Never enter credentials or secrets.)")

    ds = DashboardDataSource(
        positions_db=positions_db or None,
        ohlcv_db=ohlcv_db or None,
        quote_db=quote_db or None,
        risk_audit_path=risk_audit_path or None,
        execution_audit_path=execution_audit_path or None,
        rejection_report_path=rejection_report_path or None,
        kill_switch_file=kill_switch_file or None,
        capacity_config=capacity_config or None,
    )
    return ds, starting_capital_krw, cash_krw, since_iso


# ─────────────────────────────────────────────────
# 메인 (Main)
# ─────────────────────────────────────────────────

def main() -> None:
    """메인 앱 함수."""
    st.title("📊 JCPR Trading Dashboard")
    st.caption(
        "Task 48 v0.1.1 — 실시간 모니터링 (Real-time Monitoring) "
        "| 로컬 전용 (Local Only)"
    )

    data_source, starting_capital, cash, since_iso = _render_sidebar()

    # 5개 탭
    tabs = st.tabs([
        "📈 종합 (Overview)",
        "📂 포지션 (Positions)",
        "🛡️ 리스크 (Risk)",
        "💱 체결 (Fills)",
        "⚙️ 시스템 (System)",
    ])

    with tabs[0]:
        render_overview_view(
            positions_db=data_source.positions_db or "",
            ohlcv_db=data_source.ohlcv_db or "",
            quote_db=data_source.quote_db or "",
            starting_capital_krw=starting_capital,
            cash_krw=cash,
        )

    with tabs[1]:
        render_positions_view(
            positions_db=data_source.positions_db or "",
            ohlcv_db=data_source.ohlcv_db or "",
            quote_db=data_source.quote_db or "",
            starting_capital_krw=starting_capital,
            cash_krw=cash,
        )

    with tabs[2]:
        render_risk_view(
            risk_audit_path=data_source.risk_audit_path or "",
            since_utc_iso=since_iso,
        )

    with tabs[3]:
        render_fills_view(
            positions_db=data_source.positions_db or "",
            since_utc_iso=since_iso,
        )

    with tabs[4]:
        render_system_view(
            data_source=data_source,
            since_utc_iso=since_iso,
        )


if __name__ == "__main__":
    main()
