"""
탭 1: 종합 (Overview)
======================

JCPR Trading System - jcpr-ts-v01
Task 48 v0.1.1

핵심 KPI + Final Output #1-12 요약.
"""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import streamlit as st

from ..components import (
    build_overview_kpis,
    format_krw,
    format_pct,
    format_pnl_with_sign,
)
from ..data_loader import load_pnl_snapshot


def render_overview_view(
    *,
    positions_db: str,
    ohlcv_db: str,
    quote_db: str,
    starting_capital_krw: float,
    cash_krw: float,
) -> None:
    """종합 탭 렌더링."""
    st.header("종합 (Overview)")

    if not positions_db or not ohlcv_db:
        st.warning("⚠️ positions_db와 ohlcv_db 경로를 사이드바에서 설정하세요.")
        return

    pnl = load_pnl_snapshot(
        positions_db, ohlcv_db, quote_db,
        starting_capital_krw, cash_krw,
    )

    if "error" in pnl:
        st.error(f"PnL 계산 오류 (PnL calculation error): {pnl['error']}")
        return

    if not pnl:
        st.info("PnL 데이터 없음 — 거래 후 다시 확인하세요. (No data yet.)")
        return

    # ─── KPI 카드 ───────────────────────────────
    kpis = build_overview_kpis(pnl)
    if kpis:
        cols = st.columns(min(3, len(kpis)))
        for i, kpi in enumerate(kpis):
            with cols[i % len(cols)]:
                st.metric(
                    label=kpi["label"],
                    value=kpi["value"],
                    delta=kpi.get("delta"),
                    delta_color=kpi.get("delta_color", "normal"),
                )

    st.divider()

    # ─── 자본 변화 ─────────────────────────────
    st.subheader("자본 변화 (Capital Change)")
    capital_data = pd.DataFrame([
        {"단계 (Stage)": "시작 자본 (Starting)", "금액 (Amount KRW)": pnl["starting_capital_krw"]},
        {"단계 (Stage)": "현금 (Cash)", "금액 (Amount KRW)": pnl["cash_krw"]},
        {"단계 (Stage)": "포지션 시가 (Market Value)", "금액 (Amount KRW)": pnl["position_market_value_krw"]},
        {"단계 (Stage)": "종료 자본 (Ending)", "금액 (Amount KRW)": pnl["total_equity_krw"]},
    ])
    fig = px.bar(
        capital_data,
        x="단계 (Stage)",
        y="금액 (Amount KRW)",
        color="단계 (Stage)",
        text_auto=".0f",
    )
    fig.update_layout(showlegend=False, height=400)
    st.plotly_chart(fig, use_container_width=True)

    st.divider()

    # ─── 종목별 기여도 (Symbol attribution) ────
    st.subheader("종목별 기여도 (Symbol Attribution) — Final Output #7")
    sym_attr = pnl.get("symbol_attribution", [])
    if sym_attr:
        df = pd.DataFrame(sym_attr)
        # 표시용 컬럼 순서·이름
        display_df = df[[
            "symbol", "quantity", "avg_cost_krw", "mark_price_krw",
            "market_value_krw", "unrealized_pnl_krw", "unrealized_pnl_pct",
        ]].copy()
        display_df.columns = [
            "종목 (Symbol)", "수량 (Qty)", "평단가 (Avg Cost)", "현재가 (Mark)",
            "시가총액 (MV)", "미실현 P&L", "미실현 % (UPnL %)",
        ]
        st.dataframe(display_df, use_container_width=True, hide_index=True)

        # 차트
        if len(df) > 0:
            chart_df = df.sort_values("unrealized_pnl_krw", ascending=True)
            fig2 = px.bar(
                chart_df,
                x="unrealized_pnl_krw",
                y="symbol",
                orientation="h",
                color="unrealized_pnl_krw",
                color_continuous_scale="RdYlGn",
                title="종목별 미실현 P&L (Unrealized P&L by Symbol)",
            )
            fig2.update_layout(height=max(300, 50 * len(chart_df)))
            st.plotly_chart(fig2, use_container_width=True)
    else:
        st.info("보유 포지션 없음 (No open positions).")

    st.divider()

    # ─── 요약 텍스트 (Final Output 매핑) ───────
    st.subheader("Final Output 매핑 (Summary Mapping)")
    st.markdown(f"""
| # | 항목 | 값 |
|---|---|---|
| 1 | 시작 자본 (Starting Capital) | {format_krw(pnl['starting_capital_krw'])} |
| 2 | 종료 자본 (Ending Capital) | {format_krw(pnl['total_equity_krw'])} |
| 3 | 실현 P&L (Realized P&L) | {format_pnl_with_sign(pnl['realized_pnl_krw'])} |
| 4 | 미실현 P&L (Unrealized P&L) | {format_pnl_with_sign(pnl['unrealized_pnl_krw'])} |
| 5 | 수수료/세금 (Fees+Taxes) | {format_krw(pnl['total_fees_krw'] + pnl['total_taxes_krw'])} |
| 7 | 종목 기여도 (Symbol Attribution) | {len(sym_attr)}개 종목 (symbols) |
""")
    st.caption("※ 항목 6, 8-12는 다른 탭(Risk, Fills, System)에서 확인하세요.")
