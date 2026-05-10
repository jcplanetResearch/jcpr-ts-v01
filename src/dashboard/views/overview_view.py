"""
탭 1: 종합 (Overview)
======================

JCPR Trading System - jcpr-ts-v01
Task 48 v0.1.2 (Phase 2 A2-1 머지)

핵심 KPI + Final Output #1-12 요약.

Phase 2 A2-1 변경사항:
    - get_capacity_recommendation_status() 호출 시 config + session_summary
      + audit_summary 전달하여 capacity_advisor 자동 권장 활성화
    - placeholder 렌더 블록을 render_capacity_recommendation_section() 호출로 교체
    - Final Output 매핑 표의 #12 행을 자동 권장값 우선 표시
    - manual override 입력 옵션은 유지 (운영자 자율성 보존)
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping

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

# B4 (옵션 B-full) + Phase 2 A2-1:
#   - Strategy attribution (Final Output #6)
#   - Capacity recommendation auto-compute (Final Output #12)
from ._phase1_bridge import (
    get_capacity_recommendation_status,
    try_load_strategy_attribution,
)


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

    # ─── 전략별 기여도 (Strategy Attribution) — Final Output #6 ──
    # B4 (옵션 B-full): Phase 1 bridge 경유. v0.1.1에서는 단일 strategy
    # (momentum_v04) 가정 — Task 45 (multi-strategy registry) 완성 후
    # bridge가 자동으로 strategy_id별 분리 가능.
    st.subheader("전략별 기여도 (Strategy Attribution) — Final Output #6")
    strats = try_load_strategy_attribution(
        positions_db, ohlcv_db, quote_db,
        starting_capital_krw, cash_krw,
    )
    if strats:
        strat_df = pd.DataFrame(strats)
        # symbols 컬럼은 list — 표시용으로 ", "로 변환
        if "symbols" in strat_df.columns:
            strat_df["symbols"] = strat_df["symbols"].apply(
                lambda xs: ", ".join(xs) if isinstance(xs, list) else str(xs)
            )
        display_strat = strat_df[[
            "strategy_id", "realized_pnl_krw",
            "unrealized_pnl_krw", "fills_count", "symbols",
        ]].copy()
        display_strat.columns = [
            "전략 ID (Strategy)", "실현 P&L (Realized)",
            "미실현 P&L (Unrealized)", "체결 수 (Fills)", "종목 (Symbols)",
        ]
        st.dataframe(display_strat, use_container_width=True, hide_index=True)
        st.caption(
            "v0.1.1: 단일 strategy 가정 (momentum_v04). "
            "Task 45 multi-strategy registry 구현 후 자동 분리됨."
        )
    else:
        st.info(
            "전략별 분류 미사용 (No strategy attribution available) — "
            "포지션이 없거나 PnLEngine 미연결."
        )

    st.divider()

    # ─── 다음 세션 자본 권장 (Next-Session Capacity) — Final Output #12 ──
    # Phase 2 A2-1: capacity_advisor 자동 권장 (가능 시) + manual override fallback.
    cap_config = _try_load_capacity_config_for_overview()
    audit_summary_for_rec = {
        # TODO: A3 audit reader 통합 시 실제 EXEC_FAILED 카운트 반영
        "exec_failed_count": 0,
    }
    session_summary_for_rec = {
        "realized_pnl_krw": str(pnl.get("realized_pnl_krw", 0)),
        "unrealized_pnl_krw": str(pnl.get("unrealized_pnl_krw", 0)),
    }
    rec = get_capacity_recommendation_status(
        config=cap_config,
        session_summary=session_summary_for_rec,
        # TODO: A3 reconciliation reader 통합 시 전달
        reconciliation=None,
        audit_summary=audit_summary_for_rec,
    )
    render_capacity_recommendation_section(st, rec, format_krw=format_krw)

    # 자동 권장이 있어도 운영자 수동 override 입력 옵션은 유지
    manual_input = st.number_input(
        "수동 입력 (Manual override, KRW)",
        min_value=0.0,
        value=0.0,
        step=1_000_000.0,
        format="%.0f",
        help=(
            "자동 권장을 무시하고 운영자가 직접 결정하는 경우 입력. "
            "0이면 자동 권장 사용."
        ),
    )
    if manual_input > 0:
        st.success(
            f"✅ 수동 override (Manual override): {format_krw(manual_input)}"
        )

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
| 6 | 전략 기여도 (Strategy Attribution) | {len(strats)}개 전략 (strategies) |
| 7 | 종목 기여도 (Symbol Attribution) | {len(sym_attr)}개 종목 (symbols) |
| 12 | 다음 세션 자본 (Next Capacity) | {_format_recommendation_for_summary(rec, manual_input, format_krw)} |
""")
    st.caption("※ 항목 8-11은 다른 탭(Risk, System)에서 확인하세요.")


# ---------------------------------------------------------------------------
# Phase 2 A2-1 — capacity recommendation rendering
# ---------------------------------------------------------------------------


def render_capacity_recommendation_section(
    st: Any,                              # streamlit 모듈 (테스트 주입 가능)
    rec_status: Mapping[str, Any],
    *,
    format_krw: Any = None,              # _formatters.format_krw
) -> None:
    """`<output>` #12: 다음 세션 capacity 권장 카드.

    Args:
        st: streamlit 모듈 (or test stub).
        rec_status: get_capacity_recommendation_status() 반환 dict.
        format_krw: KRW 포매팅 함수 (없으면 단순 str(...) 사용).

    Renders:
        available=True:
            - st.metric (권장 금액 + delta)
            - rationale bullet list
            - triggers expander
        available=False:
            - st.info (reason 별 안내)
    """
    if format_krw is None:
        def format_krw(value: Any) -> str:
            return f"{value:,} KRW" if isinstance(value, (int, Decimal)) else str(value)

    available = bool(rec_status.get("available", False))
    reason = str(rec_status.get("reason", "manual"))
    recommendation = rec_status.get("recommendation")

    st.subheader("🎯 다음 세션 권장 자본 (Next-Session Capacity Recommendation)")

    if not available or recommendation is None:
        _render_unavailable(st, reason)
        return

    _render_available_card(st, recommendation, format_krw)


def _render_unavailable(st: Any, reason: str) -> None:
    """available=False 시 reason 별 안내 메시지."""
    messages = {
        "no_ladder": (
            "ℹ️ capacity.local.yaml 에 `capital_caps.ladder` 가 정의되지 않음 — "
            "운영자 설정 필요. 예시:\n\n"
            "```yaml\n"
            "capital_caps:\n"
            "  total_deployed_capital:\n"
            "    amount: 5000000\n"
            "    unit: KRW\n"
            "  ladder:\n"
            "    - 1000000\n"
            "    - 2000000\n"
            "    - 5000000\n"
            "    - 10000000\n"
            "    - 20000000\n"
            "```"
        ),
        "manual": (
            "ℹ️ 자동 권장 비활성 — 수동 입력 모드. "
            "capacity_advisor 활성화에는 ladder + session_summary + audit 데이터가 필요합니다."
        ),
        "invalid_capital": (
            "⚠️ starting_capital 값이 유효하지 않음 — "
            "capacity.local.yaml `capital_caps.total_deployed_capital.amount` 점검 필요."
        ),
    }
    msg = messages.get(reason, f"ℹ️ 권장 미가용 — reason: `{reason}`")
    st.info(msg)


def _render_available_card(
    st: Any,
    recommendation: Mapping[str, Any],
    format_krw: Any,
) -> None:
    """available=True 시 구조화 권장 카드."""
    direction = str(recommendation.get("direction", "hold"))
    direction_icon = {
        "up": "📈",
        "hold": "➡️",
        "down": "📉",
        "floor": "🚨",
    }.get(direction, "❔")
    direction_label = {
        "up": "상승",
        "hold": "유지",
        "down": "하강",
        "floor": "최하단 강제",
    }.get(direction, direction)

    # Decimal 변환 (to_dict()는 str 으로 직렬화함)
    current = _safe_decimal(recommendation.get("current_capacity_krw"))
    recommended = _safe_decimal(recommendation.get("recommended_capacity_krw"))
    delta = (
        recommended - current
        if (current is not None and recommended is not None)
        else None
    )

    col1, col2 = st.columns([1, 2])
    with col1:
        st.metric(
            label=f"권장 자본 {direction_icon} ({direction_label})",
            value=format_krw(recommended) if recommended is not None else "—",
            delta=(format_krw(delta) if delta is not None and delta != 0 else None),
        )
        step_from = recommendation.get("ladder_step_from")
        step_to = recommendation.get("ladder_step_to")
        if step_from is not None and step_to is not None:
            st.caption(f"ladder step: **{step_from}** → **{step_to}**")
        algorithm = recommendation.get("algorithm", "ladder_step_v1")
        st.caption(f"algorithm: `{algorithm}`")

    with col2:
        st.caption("**근거 (Rationale):**")
        rationale = recommendation.get("rationale", [])
        if rationale:
            for msg in rationale:
                st.write(f"• {msg}")
        else:
            st.write("• (근거 메시지 없음)")

        triggers = recommendation.get("triggers", {})
        if triggers:
            with st.expander("발화 신호 (Triggers) — 디버깅용"):
                st.json(triggers)


def _safe_decimal(value: Any) -> Decimal | None:
    """to_dict()의 str 또는 Decimal 또는 None 을 안전하게 Decimal 로."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Phase 2 A2-1 — internal helpers
# ---------------------------------------------------------------------------


def _try_load_capacity_config_for_overview():
    """capacity.local.yaml 로드 — 실패 시 None 반환 (manual fallback).

    overview_view 시그니처를 보존하기 위해 함수 내에서 직접 로드.
    실패는 silent — dashboard 가 capacity 미설정 상태에서도 동작 가능해야 함.
    """
    try:
        from src.dashboard._config import (
            load_capacity_config,
            load_dashboard_config,
        )
        dash_cfg = load_dashboard_config()
        return load_capacity_config(dash_cfg.capacity_yaml_path)
    except Exception:  # noqa: BLE001 — silent fallback
        return None


def _format_recommendation_for_summary(
    rec_status: Mapping[str, Any],
    manual_input: float,
    format_krw: Any,
) -> str:
    """Final Output 매핑 표의 #12 행 — manual override 우선, 없으면 자동 권장."""
    if manual_input > 0:
        return f"{format_krw(manual_input)} (manual override)"
    if rec_status.get("available"):
        recommendation = rec_status.get("recommendation") or {}
        amount_str = recommendation.get("recommended_capacity_krw")
        direction = recommendation.get("direction", "?")
        if amount_str:
            try:
                amount = Decimal(str(amount_str))
            except Exception:  # noqa: BLE001
                return "미입력 (not set)"
            arrow = {
                "up": "📈", "hold": "➡️", "down": "📉", "floor": "🚨"
            }.get(direction, "")
            return f"{format_krw(amount)} {arrow}"
    return "미입력 (not set)"


__all__ = (
    "render_overview_view",
    "render_capacity_recommendation_section",
)
