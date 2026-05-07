"""
스모크 테스트 — components (Smoke Tests)
=========================================

JCPR Trading System - jcpr-ts-v01
Task 48 v0.1.1
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from src.dashboard.components import (  # noqa: E402
    build_fills_summary,
    build_market_status_text,
    build_overview_kpis,
    build_risk_kpis,
    decision_label,
    format_krw,
    format_pct,
    format_pnl_with_sign,
    market_state_label,
    severity_label,
)


def test_format_krw():
    assert format_krw(10_000_000) == "10,000,000 KRW"
    assert format_krw(-50000, with_unit=False) == "-50,000"
    assert format_krw(None) == "N/A"
    assert format_krw("") == "N/A"
    assert format_krw(0) == "0 KRW"
    print("✅ test_format_krw")


def test_format_pct():
    assert format_pct(0.05) == "5.00%"
    assert format_pct(0.1234, decimals=1) == "12.3%"
    assert format_pct(None) == "N/A"
    assert format_pct(0) == "0.00%"
    print("✅ test_format_pct")


def test_format_pnl_with_sign():
    s_pos = format_pnl_with_sign(1000)
    assert s_pos.startswith("+"), f"Expected leading +, got {s_pos}"
    s_neg = format_pnl_with_sign(-1000)
    assert s_neg.startswith("-"), f"Expected leading -, got {s_neg}"
    assert format_pnl_with_sign(0).startswith("+")
    assert format_pnl_with_sign(None) == "N/A"
    print("✅ test_format_pnl_with_sign")


def test_severity_label():
    assert "심각" in severity_label("critical")
    assert "정상" in severity_label("ok")
    assert "❓" in severity_label("unknown_xyz")
    print("✅ test_severity_label")


def test_market_state_label():
    assert "정규장" in market_state_label("regular")
    assert "주말" in market_state_label("closed_weekend")
    assert "❓" in market_state_label("xyz")
    print("✅ test_market_state_label")


def test_decision_label():
    assert "승인" in decision_label("approve")
    assert "거부" in decision_label("reject")
    assert decision_label("unknown") == "unknown"
    print("✅ test_decision_label")


def test_build_overview_kpis_empty():
    assert build_overview_kpis({}) == []
    assert build_overview_kpis({"error": "x"}) == []
    print("✅ test_build_overview_kpis_empty")


def test_build_overview_kpis_full():
    pnl = {
        "starting_capital_krw": 10_000_000,
        "total_equity_krw": 10_500_000,
        "realized_pnl_krw": 100_000,
        "unrealized_pnl_krw": 400_000,
        "total_pnl_krw": 500_000,
        "total_return_pct": 5.0,
        "total_fees_krw": 1000,
        "total_taxes_krw": 0,
    }
    kpis = build_overview_kpis(pnl)
    assert len(kpis) == 6
    labels = [k["label"] for k in kpis]
    assert any("Starting" in l for l in labels)
    assert any("Ending" in l for l in labels)
    assert any("Realized" in l for l in labels)
    print("✅ test_build_overview_kpis_full")


def test_build_risk_kpis():
    summary = {
        "summary": {
            "total_evaluations": 100,
            "reject_count": 25,
            "rejection_rate": 0.25,
            "by_gate": {},
            "by_reason": {},
        },
        "diagnostic_findings": [
            {"severity": "critical", "message": "test"},
            {"severity": "warning", "message": "test"},
        ],
    }
    kpis = build_risk_kpis(summary)
    assert len(kpis) == 4
    assert "100" in kpis[0]["value"]
    assert "25" in kpis[1]["value"]
    print("✅ test_build_risk_kpis")


def test_build_fills_summary_empty():
    s = build_fills_summary(None)
    assert s["total"] == 0
    assert s["buy_count"] == 0

    s2 = build_fills_summary(pd.DataFrame())
    assert s2["total"] == 0
    print("✅ test_build_fills_summary_empty")


def test_build_fills_summary_full():
    df = pd.DataFrame([
        {"side": "buy", "quantity": 100, "gross_krw": 7_000_000, "fee_krw": 700, "tax_krw": 0},
        {"side": "buy", "quantity": 50, "gross_krw": 10_000_000, "fee_krw": 1000, "tax_krw": 0},
        {"side": "sell", "quantity": 30, "gross_krw": 2_100_000, "fee_krw": 210, "tax_krw": 4200},
    ])
    s = build_fills_summary(df)
    assert s["total"] == 3
    assert s["buy_count"] == 2
    assert s["sell_count"] == 1
    assert s["total_volume"] == 180
    assert s["total_fees_krw"] == 1910
    assert s["total_taxes_krw"] == 4200
    print("✅ test_build_fills_summary_full")


def test_build_market_status_text():
    s = build_market_status_text({"state": "regular", "is_open": True, "now_kst": "2026-05-07 14:00:00 KST"})
    assert "정규장" in s
    assert "거래 가능" in s

    s2 = build_market_status_text({"error": "fail"})
    assert "실패" in s2 or "❓" in s2

    s3 = build_market_status_text({})
    assert "❓" in s3 or "실패" in s3
    print("✅ test_build_market_status_text")


def _run_all() -> int:
    failed = 0
    tests = [
        test_format_krw,
        test_format_pct,
        test_format_pnl_with_sign,
        test_severity_label,
        test_market_state_label,
        test_decision_label,
        test_build_overview_kpis_empty,
        test_build_overview_kpis_full,
        test_build_risk_kpis,
        test_build_fills_summary_empty,
        test_build_fills_summary_full,
        test_build_market_status_text,
    ]
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            print(f"❌ {fn.__name__}: {e}")
            failed += 1
        except Exception as e:  # noqa: BLE001
            print(f"💥 {fn.__name__}: {type(e).__name__}: {e}")
            failed += 1
    return failed


if __name__ == "__main__":
    print("─" * 50)
    print("Task 48 v0.1.1 — components 스모크 테스트")
    print("─" * 50)
    failed = _run_all()
    print("─" * 50)
    if failed == 0:
        print("✅ 모든 테스트 통과 (All tests passed)")
        sys.exit(0)
    else:
        print(f"❌ {failed}개 실패 (failed)")
        sys.exit(1)
