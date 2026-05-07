"""
탭 5: 시스템 (System)
======================

JCPR Trading System - jcpr-ts-v01
Task 48 v0.1.1

시장 상태 + Kill switch + 최근 예외 + 데이터 소스 상태.
Final Output #10 (reconciliation), #11 (exceptions).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

from ..components import build_market_status_text
from ..data_loader import (
    DashboardDataSource,
    load_audit_summary,
    load_kill_switch_status,
    load_market_status,
)


def render_system_view(
    *,
    data_source: DashboardDataSource,
    since_utc_iso: str | None,
) -> None:
    """시스템 탭 렌더링."""
    st.header("시스템 (System)")

    # ─── 시장 상태 ─────────────────────────────
    st.subheader("시장 상태 (Market Status)")
    status = load_market_status()
    text = build_market_status_text(status)
    if status.get("is_open"):
        st.success(text)
    else:
        st.warning(text)

    st.divider()

    # ─── Kill Switch 상태 ──────────────────────
    st.subheader("Kill Switch 상태 (Kill Switch Status)")
    kill_active = load_kill_switch_status(data_source.kill_switch_file)
    if kill_active:
        st.error(
            f"🚨 Kill Switch ACTIVE — 모든 신규 거래 차단 (All new trades blocked)\n"
            f"파일 (File): `{data_source.kill_switch_file}`"
        )
    else:
        st.success(
            f"✅ Kill Switch INACTIVE — 정상 운영 (Normal operation)\n"
            f"감시 파일 (Watch file): `{data_source.kill_switch_file or 'not configured'}`"
        )

    st.divider()

    # ─── 데이터 소스 상태 ──────────────────────
    st.subheader("데이터 소스 상태 (Data Source Status)")
    sources = [
        ("Positions DB", data_source.positions_db),
        ("OHLCV DB", data_source.ohlcv_db),
        ("Quote DB", data_source.quote_db),
        ("Risk Audit Log", data_source.risk_audit_path),
        ("Execution Audit Log", data_source.execution_audit_path),
        ("Rejection Report", data_source.rejection_report_path),
        ("Capacity Config", data_source.capacity_config),
    ]
    rows = []
    for name, path in sources:
        if not path:
            status_str = "⚪ 미설정 (Not configured)"
            size = "—"
        else:
            p = Path(path)
            if p.exists():
                status_str = "✅ 존재 (Exists)"
                size = f"{p.stat().st_size:,} bytes"
            else:
                status_str = "❌ 없음 (Missing)"
                size = "—"
        rows.append({
            "소스 (Source)": name,
            "경로 (Path)": path or "—",
            "상태 (Status)": status_str,
            "크기 (Size)": size,
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    st.divider()

    # ─── 최근 실행 감사 (Recent Execution Audit) ──
    st.subheader("최근 실행 감사 (Recent Execution Audit) — Final Output #11")
    if data_source.execution_audit_path:
        audit_df = load_audit_summary(
            data_source.execution_audit_path,
            since_utc_iso=since_utc_iso,
            limit=100,
        )
        if audit_df.empty:
            st.info("감사 기록 없음 (No audit records).")
        else:
            # 시크릿 가능성이 있는 컬럼은 표시 안 함
            safe_cols = [
                c for c in audit_df.columns
                if not any(s in c.lower() for s in ["secret", "token", "key", "password", "auth"])
            ]
            st.dataframe(
                audit_df[safe_cols].head(50),
                use_container_width=True,
                hide_index=True,
            )
            st.caption(f"총 {len(audit_df)}건 (시크릿성 컬럼 자동 숨김 / secret-like columns auto-hidden)")
    else:
        st.info("execution_audit_path 미설정 (Not configured).")

    st.divider()

    # ─── 갱신 시각 ─────────────────────────────
    st.caption(
        f"마지막 갱신 (Last refresh): "
        f"{datetime.now(timezone.utc).isoformat()} UTC"
    )
