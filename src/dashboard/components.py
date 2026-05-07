"""
공통 UI 컴포넌트 (Common UI Components)
========================================

JCPR Trading System - jcpr-ts-v01
Task 48 v0.1.1

KPI 카드, severity 배지, 포맷팅 헬퍼.
(KPI cards, severity badges, formatting helpers.)

순수 함수 위주 — 테스트 가능 (Streamlit 호출은 view 모듈에서).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional

import pandas as pd


# ─────────────────────────────────────────────────
# 포맷팅 헬퍼 (Formatting Helpers)
# ─────────────────────────────────────────────────

def format_krw(value: Any, *, with_unit: bool = True) -> str:
    """
    KRW 정수 콤마 포맷.

    >>> format_krw(10000000)
    '10,000,000 KRW'
    >>> format_krw(-50000, with_unit=False)
    '-50,000'
    """
    if value is None or value == "" or value == "N/A":
        return "N/A"
    try:
        d = Decimal(str(value))
    except Exception:  # noqa: BLE001
        return str(value)
    sign = "-" if d < 0 else ""
    s = f"{sign}{int(abs(d)):,}"
    return f"{s} KRW" if with_unit else s


def format_pct(value: Any, decimals: int = 2) -> str:
    """
    퍼센트 포맷.

    >>> format_pct(0.05)
    '5.00%'
    >>> format_pct(0.123, decimals=1)
    '12.3%'
    """
    if value is None or value == "" or value == "N/A":
        return "N/A"
    try:
        d = Decimal(str(value))
        return f"{d * 100:.{decimals}f}%"
    except Exception:  # noqa: BLE001
        return str(value)


def format_pnl_with_sign(value: Any) -> str:
    """양수면 +, 음수면 - 명시."""
    if value is None or value == "":
        return "N/A"
    try:
        d = float(value)
    except (ValueError, TypeError):
        return str(value)
    sign = "+" if d >= 0 else ""
    return f"{sign}{format_krw(d)}"


def severity_label(severity: str) -> str:
    """severity → 이모지 + 텍스트."""
    mapping = {
        "ok": "✅ 정상 (OK)",
        "low": "🟡 주의 (LOW)",
        "moderate": "🟠 경고 (MODERATE)",
        "high": "🔴 위험 (HIGH)",
        "critical": "🚨 심각 (CRITICAL)",
        "info": "ℹ️ 정보 (INFO)",
        "warning": "⚠️ 경고 (WARNING)",
    }
    return mapping.get(severity, f"❓ {severity}")


def market_state_label(state: str) -> str:
    """시장 상태 → 한글 + 이모지."""
    mapping = {
        "regular": "🟢 정규장 (Regular)",
        "pre_market": "🟡 장전 (Pre-market)",
        "after_hours": "🟠 장후 (After hours)",
        "closed_weekend": "⚫ 주말 휴장 (Closed - Weekend)",
        "closed_holiday": "⚫ 공휴일 휴장 (Closed - Holiday)",
        "unknown": "❓ 알 수 없음 (Unknown)",
    }
    return mapping.get(state, f"❓ {state}")


def decision_label(decision: str) -> str:
    """결정 → 이모지."""
    mapping = {
        "approve": "✅ 승인",
        "reject": "❌ 거부",
        "pending": "⏳ 대기",
        "filled": "✅ 체결",
        "partial": "🟡 부분체결",
        "cancelled": "⚪ 취소",
        "rejected": "❌ 거부",
    }
    return mapping.get(decision, decision)


# ─────────────────────────────────────────────────
# KPI 빌더 (KPI Builders)
# ─────────────────────────────────────────────────

def build_overview_kpis(pnl: dict[str, Any]) -> list[dict[str, Any]]:
    """
    종합 탭 KPI 카드 데이터 (Final Output #1-7 일부).

    Returns:
        list of {label, value, delta, delta_color}
    """
    if not pnl or "error" in pnl:
        return []

    starting = pnl.get("starting_capital_krw", 0)
    ending = pnl.get("total_equity_krw", 0)
    realized = pnl.get("realized_pnl_krw", 0)
    unrealized = pnl.get("unrealized_pnl_krw", 0)
    total_pnl = pnl.get("total_pnl_krw", 0)
    return_pct = pnl.get("total_return_pct", 0)
    fees = pnl.get("total_fees_krw", 0)
    taxes = pnl.get("total_taxes_krw", 0)

    return [
        {
            "label": "시작 자본 (Starting)",
            "value": format_krw(starting),
            "delta": None,
            "delta_color": "off",
        },
        {
            "label": "종료 자본 (Ending)",
            "value": format_krw(ending),
            "delta": format_pnl_with_sign(ending - starting),
            "delta_color": "normal" if (ending - starting) >= 0 else "inverse",
        },
        {
            "label": "실현 P&L (Realized)",
            "value": format_pnl_with_sign(realized),
            "delta": None,
            "delta_color": "off",
        },
        {
            "label": "미실현 P&L (Unrealized)",
            "value": format_pnl_with_sign(unrealized),
            "delta": None,
            "delta_color": "off",
        },
        {
            "label": "총 P&L (Total)",
            "value": format_pnl_with_sign(total_pnl),
            "delta": format_pct(return_pct / 100),
            "delta_color": "normal" if total_pnl >= 0 else "inverse",
        },
        {
            "label": "수수료+세금 (Fees+Tax)",
            "value": format_krw(fees + taxes),
            "delta": None,
            "delta_color": "off",
        },
    ]


def build_risk_kpis(rejection_summary: dict[str, Any]) -> list[dict[str, Any]]:
    """
    리스크 탭 KPI 카드.
    """
    if not rejection_summary or "error" in rejection_summary:
        return []

    s = rejection_summary.get("summary", {})
    findings = rejection_summary.get("diagnostic_findings", [])
    critical_count = sum(1 for f in findings if f.get("severity") == "critical")
    warning_count = sum(1 for f in findings if f.get("severity") == "warning")

    return [
        {
            "label": "총 평가 (Total Eval)",
            "value": f"{s.get('total_evaluations', 0):,}건",
            "delta": None,
            "delta_color": "off",
        },
        {
            "label": "거부 (Rejected)",
            "value": f"{s.get('reject_count', 0):,}건",
            "delta": format_pct(s.get("rejection_rate", 0)),
            "delta_color": "inverse",
        },
        {
            "label": "Critical 진단",
            "value": f"{critical_count}건",
            "delta": None,
            "delta_color": "off",
        },
        {
            "label": "Warning 진단",
            "value": f"{warning_count}건",
            "delta": None,
            "delta_color": "off",
        },
    ]


def build_fills_summary(fills_df: Optional[pd.DataFrame]) -> dict[str, Any]:
    """체결 DataFrame → 요약 dict."""
    if fills_df is None or len(fills_df) == 0:
        return {
            "total": 0, "buy_count": 0, "sell_count": 0,
            "total_volume": 0, "total_gross_krw": 0,
            "total_fees_krw": 0, "total_taxes_krw": 0,
        }
    return {
        "total": len(fills_df),
        "buy_count": int((fills_df["side"] == "buy").sum()) if "side" in fills_df.columns else 0,
        "sell_count": int((fills_df["side"] == "sell").sum()) if "side" in fills_df.columns else 0,
        "total_volume": int(fills_df["quantity"].sum()) if "quantity" in fills_df.columns else 0,
        "total_gross_krw": float(fills_df["gross_krw"].sum()) if "gross_krw" in fills_df.columns else 0.0,
        "total_fees_krw": float(fills_df["fee_krw"].sum()) if "fee_krw" in fills_df.columns else 0.0,
        "total_taxes_krw": float(fills_df["tax_krw"].sum()) if "tax_krw" in fills_df.columns else 0.0,
    }


def build_market_status_text(status: dict[str, Any]) -> str:
    """시장 상태 → 표시 문자열."""
    if not status or "error" in status:
        return "❓ 시장 상태 조회 실패 (Market status unavailable)"
    state = status.get("state", "unknown")
    is_open = status.get("is_open", False)
    kst = status.get("now_kst", "?")
    label = market_state_label(state)
    open_text = "✅ 거래 가능 (Tradeable)" if is_open else "🔴 거래 불가 (Not tradeable)"
    return f"{label} | {open_text}\n현재 시각: {kst}"
