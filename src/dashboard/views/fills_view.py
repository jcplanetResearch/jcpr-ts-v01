"""
탭 4: 체결 (Fills)
====================

JCPR Trading System - jcpr-ts-v01
Task 48 v0.1.1

체결 이력 + 슬리피지 + 매수/매도 분포.
"""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import streamlit as st

from ..components import build_fills_summary, format_krw
from ..data_loader import load_fills


def render_fills_view(
    *,
    positions_db: str,
    since_utc_iso: str | None,
    limit: int = 500,
) -> None:
    """체결 탭 렌더링."""
    st.header("체결 (Fills)")

    if not positions_db:
        st.warning("⚠️ positions_db 경로를 사이드바에서 설정하세요.")
        return

    fills = load_fills(positions_db, since_utc_iso=since_utc_iso, limit=limit)

    if fills.empty:
        st.info("체결 기록이 없습니다 (No fills recorded).")
        return

    # ─── 요약 ──────────────────────────────────
    summary = build_fills_summary(fills)
    cols = st.columns(4)
    with cols[0]:
        st.metric("총 체결 (Total Fills)", f"{summary['total']:,}")
    with cols[1]:
        st.metric("매수 (Buy)", f"{summary['buy_count']:,}")
    with cols[2]:
        st.metric("매도 (Sell)", f"{summary['sell_count']:,}")
    with cols[3]:
        st.metric(
            "수수료+세금 (Fees+Tax)",
            format_krw(summary['total_fees_krw'] + summary['total_taxes_krw']),
        )

    st.divider()

    # ─── 체결 내역 표 ──────────────────────────
    st.subheader("체결 내역 (Fill History)")
    display = fills.copy()
    if "filled_at_utc" in display.columns:
        display["filled_at_utc"] = pd.to_datetime(display["filled_at_utc"])
    st.dataframe(display, use_container_width=True, hide_index=True)

    st.divider()

    # ─── 차트 1: 종목별 체결 수량 ──────────────
    if "symbol" in fills.columns and "quantity" in fills.columns:
        st.subheader("종목별 체결 수량 (Volume by Symbol)")
        vol_by_sym = (
            fills.groupby(["symbol", "side"])["quantity"]
            .sum()
            .reset_index()
        )
        fig = px.bar(
            vol_by_sym, x="symbol", y="quantity", color="side",
            barmode="group", text_auto=True,
        )
        fig.update_layout(height=400)
        st.plotly_chart(fig, use_container_width=True)

    st.divider()

    # ─── 차트 2: 체결 시간 분포 ────────────────
    if "filled_at_utc" in fills.columns:
        st.subheader("체결 시간 분포 (Fills Over Time)")
        time_df = fills.copy()
        time_df["filled_at_utc"] = pd.to_datetime(time_df["filled_at_utc"])
        # 30분 단위 버킷
        time_df["bucket"] = time_df["filled_at_utc"].dt.floor("30min")
        bucket = time_df.groupby(["bucket", "side"]).size().reset_index(name="count")
        fig = px.line(
            bucket, x="bucket", y="count", color="side",
            markers=True,
        )
        fig.update_layout(height=350)
        st.plotly_chart(fig, use_container_width=True)

    st.divider()

    # ─── 슬리피지 (간단 버전 — 의도가 데이터 있을 때만) ─
    st.subheader("수수료/세금 분석 (Fees & Taxes)")
    if "fee_krw" in fills.columns and "tax_krw" in fills.columns:
        fee_summary = pd.DataFrame([
            {"종류 (Type)": "수수료 (Fees)", "금액 (KRW)": float(fills["fee_krw"].sum())},
            {"종류 (Type)": "세금 (Taxes)", "금액 (KRW)": float(fills["tax_krw"].sum())},
        ])
        fig = px.pie(fee_summary, names="종류 (Type)", values="금액 (KRW)", hole=0.4)
        fig.update_layout(height=300)
        st.plotly_chart(fig, use_container_width=True)
