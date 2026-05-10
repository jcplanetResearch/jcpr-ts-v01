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
# B-full additions (옵션 B-full):
#   B1: layer 13-17 보안 게이트 호출
#   B5: capacity.local.yaml 자동 default 로드
from src.dashboard._security import (  # noqa: E402
    DashboardSecurityError,
    assert_audit_logs_secured,
    assert_no_secrets_in_env,
    enforce_localhost_binding,
    verify_db_permissions,
)
from src.dashboard.views._phase1_bridge import (  # noqa: E402
    try_load_capacity_default,
)
from src.dashboard._sidebar_defaults import resolve_sidebar_defaults

# ─────────────────────────────────────────────────
# A1 (옵션 Y): 데이터 소스 권한 검증 헬퍼
# ─────────────────────────────────────────────────

def _verify_data_source_permissions(
    data_source: DashboardDataSource,
) -> list[tuple[str, str]]:
    """모든 입력 경로의 권한 검증; 위반 사항 리스트 반환.

    DB(layer 15) + audit log(layer 17) 모두 검사. 위반 사항은 raise
    하지 않고 리스트로 반환 → 호출자가 사이드바에 일괄 표시. 데이터
    로드는 graceful 동작 (`_safe_query`가 권한 위반 시 빈 DataFrame).

    Returns:
        list of (label, error_message) tuples. Empty list = all OK.
    """
    issues: list[tuple[str, str]] = []

    # DB 경로들 (layer 15)
    db_paths = [
        ("Positions DB", data_source.positions_db),
        ("OHLCV DB", data_source.ohlcv_db),
        ("Quote DB", data_source.quote_db),
    ]
    for label, raw in db_paths:
        if not raw:
            continue
        try:
            verify_db_permissions(Path(raw))
        except DashboardSecurityError as exc:
            issues.append((label, str(exc)))

    # Audit 로그들 (layer 17) — 일괄 검사
    audit_paths: list[Path] = []
    audit_label_map: dict[str, str] = {}
    for label, raw in [
        ("Risk Audit", data_source.risk_audit_path),
        ("Execution Audit", data_source.execution_audit_path),
        ("Reconciliation Audit", data_source.reconciliation_audit_path),
    ]:
        if raw:
            p = Path(raw)
            audit_paths.append(p)
            audit_label_map[str(p)] = label
    if audit_paths:
        try:
            assert_audit_logs_secured(audit_paths)
        except DashboardSecurityError as exc:
            # assert_audit_logs_secured raises on first violation; we
            # extract the offending path from the message and tag it.
            msg = str(exc)
            tagged_label = "Audit Logs"
            for path_str, label in audit_label_map.items():
                if path_str in msg:
                    tagged_label = label
                    break
            issues.append((tagged_label, msg))

    return issues


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
    # Phase 2 — Sidebar Automation: 3-tier fallback chain default
    defaults = resolve_sidebar_defaults()

    st.sidebar.title("⚙️ 설정 (Settings)")
    st.sidebar.caption("JCPR Trading System v0.1.1")

    # 데이터 소스 경로
    st.sidebar.subheader("📁 데이터 소스 (Data Sources)")

    positions_db = st.sidebar.text_input(
        "Positions DB",
        value=defaults.positions_db,
    	help=f"auto-loaded from: {defaults.sources['positions_db']}",
    )
    ohlcv_db = st.sidebar.text_input(
        "OHLCV DB",
        value=defaults.ohlcv_db,
    	help=f"auto-loaded from: {defaults.sources['ohlcv_db']}",
    )
    quote_db = st.sidebar.text_input(
        "Quote DB",
        value=defaults.quote_db,
    	help=f"auto-loaded from: {defaults.sources['quote_db']}",
    )
    risk_audit_path = st.sidebar.text_input(
        "Risk Audit Log (.jsonl)",
        value=defaults.risk_audit_path,
    	help=f"auto-loaded from: {defaults.sources['risk_audit_path']}",
    )
    execution_audit_path = st.sidebar.text_input(
        "Execution Audit Log (.jsonl)",
        value=defaults.execution_audit_path,
    	help=f"auto-loaded from: {defaults.sources['execution_audit_path']}",
    )
    # A3 (옵션 Y): Reconciler가 작성한 jsonl 경로. 별도 프로세스가
    # KIS API와 통신하여 결과를 여기에 append → dashboard는 read-only.
    reconciliation_audit_path = st.sidebar.text_input(
        "Reconciliation Audit (.jsonl)",
        value=defaults.reconciliation_audit_path,
    	help=f"auto-loaded from: {defaults.sources['reconciliation_audit_path']}",
    )
    rejection_report_path = st.sidebar.text_input(
        "Rejection Report (선택)",
        value=defaults.rejection_report_path,
    	help=f"auto-loaded from: {defaults.sources['rejection_report_path']}",
    )
    kill_switch_file = st.sidebar.text_input(
        "Kill Switch File",
        value=defaults.kill_switch_file,
    	help=f"auto-loaded from: {defaults.sources['kill_switch_file']}",
    )
    capacity_config = st.sidebar.text_input(
        "Capacity Config",
        value=defaults.capacity_config,
    	help=f"auto-loaded from: {defaults.sources['capacity_config']}",
    )

    st.sidebar.divider()

    # B5 (옵션 B-full): capacity.local.yaml 자동 default 로드
    # 운영자가 명시적으로 다른 값을 입력하면 그것이 우선 — 본 자동 로드는
    # default suggestion만 제공.
    capacity_loaded = try_load_capacity_default(capacity_config)
    if capacity_loaded is not None:
        st.sidebar.caption(
            f"✅ capacity 로드됨 (loaded): "
            f"profile=`{capacity_loaded['profile_name']}`, "
            f"mode=`{capacity_loaded['operating_mode']}`"
        )
        default_starting = capacity_loaded["starting_capital_krw"]
    else:
        default_starting = float(os.environ.get("JCPR_STARTING_CAPITAL", "10000000"))

    # 세션 정보
    st.sidebar.subheader("💰 세션 정보 (Session)")
    starting_capital_krw = st.sidebar.number_input(
        "시작 자본 (Starting Capital, KRW)",
        min_value=0.0,
        value=defaults.starting_capital_krw,
    	help=f"auto-loaded from: {defaults.sources['starting_capital_krw']}",
        step=1_000_000.0,
        format="%.0f",
    )
    cash_krw = st.sidebar.number_input(
        "현금 잔고 (Cash Balance, KRW)",
        min_value=0.0,
        # value=float(os.environ.get("JCPR_CASH", "10000000")),
	value=defaults.cash_krw,
    	help=f"auto-loaded from: {defaults.sources['cash_krw']}",
        step=100_000.0,
        format="%.0f",
        # help="브로커 API에서 자동 갱신될 예정",
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
        reconciliation_audit_path=reconciliation_audit_path or None,  # A3
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

    # B1 (옵션 B-full): 보안 게이트 layer 13-17 호출.
    # st.stop()은 이후 모든 컨텐츠 렌더를 차단하므로, 위반 시 운영자는
    # 명시적 에러만 보고 데이터에 접근하지 못함 (fail-closed).
    try:
        assert_no_secrets_in_env()
        # Streamlit이 이미 시작되어 있으므로 사후 검증; 잘못된 바인딩이면
        # 화면에 명시적으로 표시하고 컨텐츠 렌더 차단.
        enforce_localhost_binding(
            os.environ.get("STREAMLIT_SERVER_ADDRESS"),
        )
    except DashboardSecurityError as exc:
        st.error(f"🛑 보안 검증 실패 (Security check failed): {exc}")
        st.caption(
            "대시보드를 안전하게 시작하려면:\n"
            "1. `streamlit run ... --server.address=127.0.0.1` 으로 실행 "
            "(또는 환경변수 `STREAMLIT_SERVER_ADDRESS=127.0.0.1`)\n"
            "2. 셸에 `PASSWORD=`, `TOKEN=`, `API_KEY=` 같은 raw 시크릿 "
            "env var이 없어야 함\n"
            "3. 시크릿은 반드시 `.env` 파일을 통해 로드 (직접 export 금지)"
        )
        st.stop()  # 이후 컨텐츠 렌더 차단 (fail-closed)

    data_source, starting_capital, cash, since_iso = _render_sidebar()

    # A1 (옵션 Y): layer 15 (DB) + layer 17 (audit) 권한 일괄 검증.
    # 위반 시 raise 안 함 — 화면에 경고만 표시하고 `_safe_query`가 빈
    # DataFrame을 반환하여 graceful 처리. 운영자는 `chmod 600 <path>`로
    # 즉시 복구 가능.
    perm_issues = _verify_data_source_permissions(data_source)
    if perm_issues:
        st.warning(
            "⚠️ **데이터 소스 권한 위반 (Permission violations detected)** — "
            "0600(rw-------)이 아닌 파일이 있습니다. "
            "권한 위반 파일은 데이터 로드가 차단됩니다 (fail-closed)."
        )
        for label, msg in perm_issues:
            st.error(f"**{label}**: {msg}")
        st.code(
            "# 모든 데이터 파일을 0600으로 보호:\n"
            "chmod 600 data/approvals.sqlite\n"
            "chmod 600 data/audit/*.jsonl\n"
            "# 새로고침: 사이드바 🔄 새로고침 또는 페이지 reload",
            language="bash",
        )
        st.divider()

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
