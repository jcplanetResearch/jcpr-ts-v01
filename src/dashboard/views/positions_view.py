"""
탭 2: 포지션 (Positions)
=========================

JCPR Trading System - jcpr-ts-v01
Task 48 v0.1.1

현재 보유 포지션 + 종목별 P&L + 시장가치 차트.
"""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import streamlit as st

from ..components import format_krw, format_pnl_with_sign
from ..data_loader import load_pnl_snapshot, load_positions


def render_positions_view(
    *,
    positions_db: str,
    ohlcv_db: str,
    quote_db: str,
    starting_capital_krw: float,
    cash_krw: float,
) -> None:
    """포지션 탭 렌더링."""
    st.header("포지션 (Positions)")

    if not positions_db:
        st.warning("⚠️ positions_db 경로를 사이드바에서 설정하세요.")
        return

    positions = load_positions(positions_db)

    if positions.empty:
        st.info("현재 보유 포지션이 없습니다 (No open positions).")
        return

    # ─── 요약 통계 ─────────────────────────────
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("총 종목 수 (Symbols)", f"{len(positions)}")
    with col2:
        long_count = int((positions["side"] == "long").sum()) if "side" in positions.columns else 0
        st.metric("롱 포지션 (Long)", f"{long_count}")
    with col3:
        short_count = int((positions["side"] == "short").sum()) if "side" in positions.columns else 0
        st.metric("숏 포지션 (Short)", f"{short_count}")

    st.divider()

    # ─── 상세 표 (mark price 포함) ──────────────
    st.subheader("포지션 상세 (Position Details)")
    pnl = load_pnl_snapshot(
        positions_db, ohlcv_db, quote_db,
        starting_capital_krw, cash_krw,
    )

    if "error" in pnl:
        st.error(f"P&L 계산 오류: {pnl['error']}")
        st.dataframe(positions, use_container_width=True, hide_index=True)
        return

    sym_attr = pnl.get("symbol_attribution", [])
    if sym_attr:
        df = pd.DataFrame(sym_attr)
        df["cost_basis_krw"] = df["quantity"] * df["avg_cost_krw"]

        display_df = df[[
            "symbol", "quantity", "avg_cost_krw", "mark_price_krw",
            "cost_basis_krw", "market_value_krw",
            "unrealized_pnl_krw", "unrealized_pnl_pct",
        ]].copy()
        display_df.columns = [
            "종목", "수량", "평단가 (KRW)", "현재가 (KRW)",
            "매입가액 (KRW)", "시가총액 (KRW)",
            "미실현 P&L (KRW)", "미실현 % ",
        ]
        st.dataframe(display_df, use_container_width=True, hide_index=True)

        st.divider()

        # ─── 차트 1: 시가총액 vs 매입가액 ─────────
        st.subheader("시가총액 vs 매입가액 (Market Value vs Cost Basis)")
        compare_df = pd.DataFrame({
            "symbol": df["symbol"].tolist() * 2,
            "type": ["매입가액 (Cost)"] * len(df) + ["시가총액 (Market)"] * len(df),
            "amount_krw": df["cost_basis_krw"].tolist() + df["market_value_krw"].tolist(),
        })
        fig1 = px.bar(
            compare_df, x="symbol", y="amount_krw", color="type",
            barmode="group", text_auto=".0f",
        )
        fig1.update_layout(height=400)
        st.plotly_chart(fig1, use_container_width=True)

        st.divider()

        # ─── 차트 2: 포트폴리오 비중 (도넛) ─────
        st.subheader("포트폴리오 비중 (Portfolio Allocation)")
        fig2 = px.pie(
            df, names="symbol", values="market_value_krw",
            hole=0.5,
        )
        fig2.update_layout(height=400)
        st.plotly_chart(fig2, use_container_width=True)
    else:
        st.dataframe(positions, use_container_width=True, hide_index=True)
