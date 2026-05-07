"""
탭 3: 리스크 (Risk)
====================

JCPR Trading System - jcpr-ts-v01
Task 48 v0.1.1

리스크 게이트 거부 분석 + 진단 + 30분 윈도우 추세.
Final Output #8 (rejected orders), #9 (risk-limit usage).
"""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import streamlit as st

from ..components import build_risk_kpis, format_pct, severity_label
from ..data_loader import load_rejection_summary


def render_risk_view(
    *,
    risk_audit_path: str,
    since_utc_iso: str | None,
) -> None:
    """리스크 탭 렌더링."""
    st.header("리스크 (Risk)")

    if not risk_audit_path:
        st.warning("⚠️ risk_audit_path 경로를 사이드바에서 설정하세요.")
        return

    summary = load_rejection_summary(risk_audit_path, since_utc_iso=since_utc_iso)

    if "error" in summary:
        st.error(f"거부 요약 오류 (Rejection summary error): {summary['error']}")
        return

    s = summary.get("summary", {})
    if s.get("total_evaluations", 0) == 0:
        st.info("평가 기록이 없습니다 (No evaluation records).")
        return

    # ─── KPI 카드 ───────────────────────────────
    kpis = build_risk_kpis(summary)
    if kpis:
        cols = st.columns(len(kpis))
        for i, kpi in enumerate(kpis):
            with cols[i]:
                st.metric(
                    label=kpi["label"],
                    value=kpi["value"],
                    delta=kpi.get("delta"),
                    delta_color=kpi.get("delta_color", "normal"),
                )

    st.divider()

    # ─── 진단 (Diagnostics) ────────────────────
    findings = summary.get("diagnostic_findings", [])
    if findings:
        st.subheader("진단 (Diagnostic Findings)")
        for f in findings:
            sev = f.get("severity", "info")
            msg = f.get("message", "")
            label = severity_label(sev)
            if sev == "critical":
                st.error(f"{label}: {msg}")
            elif sev == "warning":
                st.warning(f"{label}: {msg}")
            else:
                st.info(f"{label}: {msg}")
        st.divider()

    # ─── 게이트별 거부 분포 ────────────────────
    by_gate = s.get("by_gate", {})
    by_reason = s.get("by_reason", {})

    col1, col2 = st.columns(2)

    with col1:
        st.subheader("게이트별 거부 (By Gate)")
        if by_gate:
            gate_df = pd.DataFrame([
                {"게이트 (Gate)": k, "거부 건수 (Count)": v}
                for k, v in sorted(by_gate.items(), key=lambda x: -x[1])
            ])
            fig = px.bar(
                gate_df, x="게이트 (Gate)", y="거부 건수 (Count)",
                color="거부 건수 (Count)", color_continuous_scale="Reds",
                text_auto=True,
            )
            fig.update_layout(height=350, showlegend=False)
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("거부된 주문 없음 (No rejected orders).")

    with col2:
        st.subheader("사유별 거부 (By Reason)")
        if by_reason:
            reason_df = pd.DataFrame([
                {"사유 (Reason)": k, "건수 (Count)": v}
                for k, v in sorted(by_reason.items(), key=lambda x: -x[1])[:10]
            ])
            fig = px.bar(
                reason_df, x="건수 (Count)", y="사유 (Reason)",
                orientation="h", color="건수 (Count)",
                color_continuous_scale="Oranges", text_auto=True,
            )
            fig.update_layout(height=350, showlegend=False)
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("거부 사유 없음 (No rejection reasons).")

    st.divider()

    # ─── 30분 윈도우 추세 ──────────────────────
    st.subheader("거부율 추세 (Rejection Rate Trend — 30min Window)")
    trend = summary.get("window_30min_trend", [])
    if trend:
        trend_df = pd.DataFrame(trend)
        trend_df["window_start"] = pd.to_datetime(trend_df["window_start"])
        trend_df["rate_pct"] = trend_df["rate"] * 100

        fig = px.line(
            trend_df, x="window_start", y="rate_pct",
            markers=True, title="거부율 % (Rejection Rate %)",
        )
        fig.add_scatter(
            x=trend_df["window_start"], y=trend_df["count"],
            name="총 평가 (Total Eval)", yaxis="y2", mode="lines",
            line={"dash": "dot"},
        )
        fig.update_layout(
            height=400,
            yaxis={"title": "거부율 % (Rate %)"},
            yaxis2={"title": "건수 (Count)", "overlaying": "y", "side": "right"},
            legend={"x": 0.01, "y": 0.99},
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("추세 데이터 부족 (Insufficient trend data).")
