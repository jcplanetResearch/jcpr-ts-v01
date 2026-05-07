"""
스모크 테스트 — capacity_recommender
=====================================

JCPR Trading System - jcpr-ts-v01
Task 49 v0.2

5단계 추천 알고리즘 검증.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from src.reports.capacity_recommender import (  # noqa: E402
    CapacityThresholds,
    recommend_next_capacity,
)


def test_clean_session_no_signals():
    """무문제 + 0 P&L → hold."""
    rec = recommend_next_capacity(
        starting_capital_krw=Decimal("10000000"),
        realized_pnl_krw=Decimal("0"),
        unrealized_pnl_krw=Decimal("0"),
    )
    assert rec.stage == "hold"
    assert rec.risk_signals == 0
    assert rec.multiplier == "1.00"
    print("✅ test_clean_session_no_signals")


def test_profitable_session_increase():
    """무문제 + 5% 수익 → increase."""
    rec = recommend_next_capacity(
        starting_capital_krw=Decimal("10000000"),
        realized_pnl_krw=Decimal("500000"),
        unrealized_pnl_krw=Decimal("0"),
    )
    assert rec.stage == "increase", f"Got {rec.stage}"
    assert rec.risk_signals == 0
    print("✅ test_profitable_session_increase")


def test_warning_rejection_rate():
    """30% 거부율 → warning → reduce_mild."""
    rec = recommend_next_capacity(
        starting_capital_krw=Decimal("10000000"),
        realized_pnl_krw=Decimal("0"),
        unrealized_pnl_krw=Decimal("0"),
        rejection_rate=0.35,
    )
    assert rec.stage == "reduce_mild"
    assert rec.risk_signals >= 1
    print("✅ test_warning_rejection_rate")


def test_critical_rejection_rate():
    """50% 거부율 → critical → reduce_strong."""
    rec = recommend_next_capacity(
        starting_capital_krw=Decimal("10000000"),
        realized_pnl_krw=Decimal("0"),
        unrealized_pnl_krw=Decimal("0"),
        rejection_rate=0.55,
    )
    assert rec.stage == "reduce_strong"
    print("✅ test_critical_rejection_rate")


def test_major_reconciliation_halt():
    """정합성 major → halt."""
    rec = recommend_next_capacity(
        starting_capital_krw=Decimal("10000000"),
        realized_pnl_krw=Decimal("0"),
        unrealized_pnl_krw=Decimal("0"),
        reconciliation_severity="major",
    )
    assert rec.stage == "halt"
    assert rec.recommended_capital_krw == "0"
    print("✅ test_major_reconciliation_halt")


def test_critical_loss():
    """5% 이상 손실 → critical → reduce_strong."""
    rec = recommend_next_capacity(
        starting_capital_krw=Decimal("10000000"),
        realized_pnl_krw=Decimal("-600000"),
        unrealized_pnl_krw=Decimal("0"),
    )
    assert rec.stage == "reduce_strong"
    print("✅ test_critical_loss")


def test_multiple_warnings_escalate():
    """여러 warning → 누적되어도 reduce_mild 유지 (강한 신호 없음)."""
    rec = recommend_next_capacity(
        starting_capital_krw=Decimal("10000000"),
        realized_pnl_krw=Decimal("-300000"),  # warning
        unrealized_pnl_krw=Decimal("0"),
        rejection_rate=0.35,  # warning
        exception_count=1,  # warning
    )
    assert rec.stage == "reduce_mild"
    assert rec.risk_signals == 3
    print("✅ test_multiple_warnings_escalate")


def test_critical_overrides_warning():
    """critical 1개 + warning 다수 → reduce_strong."""
    rec = recommend_next_capacity(
        starting_capital_krw=Decimal("10000000"),
        realized_pnl_krw=Decimal("-100000"),  # warning
        unrealized_pnl_krw=Decimal("0"),
        rejection_rate=0.60,  # critical
    )
    assert rec.stage == "reduce_strong"
    print("✅ test_critical_overrides_warning")


def test_custom_thresholds():
    """thresholds 주입."""
    custom = CapacityThresholds(
        rejection_rate_high=0.10,  # 더 엄격
        rejection_rate_critical=0.20,
    )
    # 11% → 새 임계값에서는 high
    rec = recommend_next_capacity(
        starting_capital_krw=Decimal("10000000"),
        realized_pnl_krw=Decimal("0"),
        unrealized_pnl_krw=Decimal("0"),
        rejection_rate=0.11,
        thresholds=custom,
    )
    assert rec.stage == "reduce_mild"
    print("✅ test_custom_thresholds")


def test_recommended_capital_arithmetic():
    """추천 자본 = 시작 × multiplier."""
    rec = recommend_next_capacity(
        starting_capital_krw=Decimal("10000000"),
        realized_pnl_krw=Decimal("0"),
        unrealized_pnl_krw=Decimal("0"),
        rejection_rate=0.55,  # critical → 0.50배
    )
    assert rec.recommended_capital_krw == "5000000", f"Got {rec.recommended_capital_krw}"
    print("✅ test_recommended_capital_arithmetic")


def test_to_dict_serializable():
    """to_dict — 모든 값 직렬화 가능."""
    import json
    rec = recommend_next_capacity(
        starting_capital_krw=Decimal("10000000"),
        realized_pnl_krw=Decimal("0"),
        unrealized_pnl_krw=Decimal("0"),
    )
    d = rec.to_dict()
    s = json.dumps(d)
    assert "stage" in s
    print("✅ test_to_dict_serializable")


def _run_all() -> int:
    failed = 0
    tests = [
        test_clean_session_no_signals,
        test_profitable_session_increase,
        test_warning_rejection_rate,
        test_critical_rejection_rate,
        test_major_reconciliation_halt,
        test_critical_loss,
        test_multiple_warnings_escalate,
        test_critical_overrides_warning,
        test_custom_thresholds,
        test_recommended_capital_arithmetic,
        test_to_dict_serializable,
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
    print("Task 49 v0.2 — capacity_recommender 스모크 테스트")
    print("─" * 50)
    failed = _run_all()
    print("─" * 50)
    if failed == 0:
        print("✅ 모든 테스트 통과")
        sys.exit(0)
    else:
        print(f"❌ {failed}개 실패")
        sys.exit(1)
