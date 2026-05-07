"""
스모크 테스트 — portfolio_risk
================================

JCPR Trading System - jcpr-ts-v01
Task 47 v0.2
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from src.risk import (  # noqa: E402
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    SEVERITY_WARNING,
    PortfolioRiskAnalyzer,
    PortfolioRiskConfig,
    PortfolioRiskSnapshot,
    ProjectedImpact,
    quick_analyze,
)


# ─────────────────────────────────────────────────
# 헬퍼 (Helpers)
# ─────────────────────────────────────────────────

def _analyzer(**config_kwargs) -> PortfolioRiskAnalyzer:
    sector_map = {
        "005930": "tech", "000660": "tech", "035420": "tech",
        "005380": "auto", "012330": "auto",
        "069500": "etf", "091160": "etf",
        "068270": "healthcare",
    }
    return PortfolioRiskAnalyzer(
        sector_map=sector_map,
        config=PortfolioRiskConfig(**config_kwargs),
    )


# ─────────────────────────────────────────────────
# 설정 (Config)
# ─────────────────────────────────────────────────

def test_config_defaults():
    c = PortfolioRiskConfig()
    assert c.max_total_exposure_pct == Decimal("0.80")
    assert c.exempt_etf_from_sector is True
    print("✅ test_config_defaults")


def test_config_invalid_pct():
    """0 또는 1 초과 거부."""
    for bad in [Decimal("0"), Decimal("-0.1"), Decimal("1.5")]:
        try:
            PortfolioRiskConfig(max_total_exposure_pct=bad)
            assert False
        except ValueError:
            pass
    print("✅ test_config_invalid_pct")


def test_config_invalid_diversification():
    try:
        PortfolioRiskConfig(sector_min_diversification=0)
        assert False
    except ValueError:
        pass
    print("✅ test_config_invalid_diversification")


# ─────────────────────────────────────────────────
# 기본 분석 (Basic Analyze)
# ─────────────────────────────────────────────────

def test_empty_positions():
    """빈 포지션 — 모두 현금."""
    a = _analyzer()
    snap = a.analyze(positions={}, equity_krw=Decimal("10000000"))
    assert snap.total_exposure_krw == Decimal(0)
    assert snap.total_exposure_pct == Decimal(0)
    assert snap.cash_pct == Decimal("1.0000")
    assert snap.symbol_count == 0
    assert snap.severity == SEVERITY_OK
    print("✅ test_empty_positions")


def test_single_position_under_limits():
    """단일 포지션 — 한도 내."""
    a = _analyzer()
    pos = {
        "005930": {"market_value_krw": Decimal("1000000")},  # 10%
    }
    snap = a.analyze(positions=pos, equity_krw=Decimal("10000000"))
    assert snap.symbol_count == 1
    assert snap.sector_count == 1
    assert snap.severity == SEVERITY_OK or snap.severity == SEVERITY_WARNING
    # sector_min_diversification=2이므로 1개 섹터 → warning
    assert snap.severity == SEVERITY_WARNING
    assert any("분산 부족" in w for w in snap.warnings)
    print("✅ test_single_position_under_limits")


def test_total_exposure_warning():
    """전체 노출 한도 초과."""
    a = _analyzer(max_total_exposure_pct=Decimal("0.50"))
    pos = {
        "005930": {"market_value_krw": Decimal("3000000")},
        "069500": {"market_value_krw": Decimal("3000000")},  # ETF
    }
    snap = a.analyze(positions=pos, equity_krw=Decimal("10000000"))
    # 60% > 50% 한도
    assert any("전체 노출" in w for w in snap.warnings)
    assert snap.severity in (SEVERITY_WARNING, SEVERITY_CRITICAL)
    print("✅ test_total_exposure_warning")


def test_single_symbol_warning():
    """단일 종목 한도 초과."""
    a = _analyzer(max_single_symbol_pct=Decimal("0.10"))
    pos = {
        "005930": {"market_value_krw": Decimal("2000000")},  # 20%
    }
    snap = a.analyze(positions=pos, equity_krw=Decimal("10000000"))
    assert any("종목 005930" in w for w in snap.warnings)
    print("✅ test_single_symbol_warning")


def test_sector_concentration_warning():
    """섹터 집중도 초과."""
    a = _analyzer(max_sector_exposure_pct=Decimal("0.30"))
    pos = {
        "005930": {"market_value_krw": Decimal("2000000")},  # tech
        "000660": {"market_value_krw": Decimal("2000000")},  # tech
        "035420": {"market_value_krw": Decimal("1000000")},  # tech
        # tech 합 = 5M = 50% > 30% 한도
        "005380": {"market_value_krw": Decimal("1000000")},  # auto
    }
    snap = a.analyze(positions=pos, equity_krw=Decimal("10000000"))
    assert any("섹터 tech" in w for w in snap.warnings)
    print("✅ test_sector_concentration_warning")


def test_etf_exempt_from_sector():
    """ETF는 섹터 집중도에서 면제."""
    a = _analyzer(
        max_sector_exposure_pct=Decimal("0.30"),
        sector_min_diversification=1,
    )
    pos = {
        "069500": {"market_value_krw": Decimal("5000000")},  # ETF 50%
        "005930": {"market_value_krw": Decimal("1000000")},  # tech
    }
    snap = a.analyze(positions=pos, equity_krw=Decimal("10000000"))
    # ETF 50%여도 면제 → 섹터 경고 없어야
    sec_warns = [w for w in snap.warnings if "섹터 etf" in w]
    assert len(sec_warns) == 0, f"ETF should be exempt, got {sec_warns}"
    print("✅ test_etf_exempt_from_sector")


def test_etf_not_exempt_when_disabled():
    """exempt_etf_from_sector=False 시 ETF도 검사."""
    a = _analyzer(
        max_sector_exposure_pct=Decimal("0.30"),
        exempt_etf_from_sector=False,
        sector_min_diversification=1,
    )
    pos = {
        "069500": {"market_value_krw": Decimal("5000000")},  # ETF 50%
    }
    snap = a.analyze(positions=pos, equity_krw=Decimal("10000000"))
    assert any("섹터 etf" in w for w in snap.warnings)
    print("✅ test_etf_not_exempt_when_disabled")


def test_diversification_warning():
    """섹터 분산 부족 — 1개 섹터만 있을 때."""
    a = _analyzer(sector_min_diversification=3)
    pos = {
        "005930": {"market_value_krw": Decimal("1000000")},
        "000660": {"market_value_krw": Decimal("1000000")},
    }
    snap = a.analyze(positions=pos, equity_krw=Decimal("10000000"))
    # 둘 다 tech → 1개 섹터 < 3개
    assert any("분산 부족" in w for w in snap.warnings)
    print("✅ test_diversification_warning")


def test_unknown_sector_handling():
    """sector_map에 없는 종목 → unknown."""
    a = _analyzer()
    pos = {
        "999999": {"market_value_krw": Decimal("1000000")},
    }
    snap = a.analyze(positions=pos, equity_krw=Decimal("10000000"))
    sectors = [b["sector"] for b in snap.by_sector]
    assert "unknown" in sectors
    print("✅ test_unknown_sector_handling")


# ─────────────────────────────────────────────────
# 측정 (Metrics)
# ─────────────────────────────────────────────────

def test_hhi_concentrated():
    """1종목 집중 → HHI 10000."""
    a = _analyzer(sector_min_diversification=1)
    pos = {"005930": {"market_value_krw": Decimal("1000000")}}
    snap = a.analyze(positions=pos, equity_krw=Decimal("10000000"))
    assert snap.hhi == Decimal("10000.00")
    print("✅ test_hhi_concentrated")


def test_hhi_diversified():
    """4종목 균등 → HHI 2500."""
    a = _analyzer()
    pos = {
        "005930": {"market_value_krw": Decimal("1000000")},
        "000660": {"market_value_krw": Decimal("1000000")},
        "005380": {"market_value_krw": Decimal("1000000")},
        "068270": {"market_value_krw": Decimal("1000000")},
    }
    snap = a.analyze(positions=pos, equity_krw=Decimal("10000000"))
    # 각 25% → 4 * 0.25^2 = 0.25 → HHI 2500
    assert snap.hhi == Decimal("2500.00"), f"Got {snap.hhi}"
    print("✅ test_hhi_diversified")


# ─────────────────────────────────────────────────
# Edge cases
# ─────────────────────────────────────────────────

def test_zero_equity_with_exposure():
    """equity=0인데 노출 있음 → critical."""
    a = _analyzer()
    pos = {"005930": {"market_value_krw": Decimal("1000000")}}
    snap = a.analyze(positions=pos, equity_krw=Decimal(0))
    assert snap.severity == SEVERITY_CRITICAL
    assert any("자본 ≤ 0" in w for w in snap.warnings)
    print("✅ test_zero_equity_with_exposure")


def test_zero_market_value_skipped():
    """market_value_krw=0 인 항목은 skip."""
    a = _analyzer()
    pos = {
        "005930": {"market_value_krw": Decimal("1000000")},
        "000660": {"market_value_krw": Decimal(0)},  # skip
    }
    snap = a.analyze(positions=pos, equity_krw=Decimal("10000000"))
    assert snap.symbol_count == 1
    print("✅ test_zero_market_value_skipped")


def test_invalid_market_value_treated_as_zero():
    """파싱 불가능 값 → 0 처리."""
    a = _analyzer()
    pos = {
        "005930": {"market_value_krw": "invalid"},
    }
    snap = a.analyze(positions=pos, equity_krw=Decimal("10000000"))
    assert snap.symbol_count == 0
    print("✅ test_invalid_market_value_treated_as_zero")


# ─────────────────────────────────────────────────
# Project (가상 시나리오)
# ─────────────────────────────────────────────────

def test_project_buy_increases_exposure():
    """매수 후 노출 증가."""
    a = _analyzer()
    pos = {
        "005930": {"market_value_krw": Decimal("1000000")},
        "005380": {"market_value_krw": Decimal("1000000")},
    }
    impact = a.project(
        current_positions=pos,
        equity_krw=Decimal("10000000"),
        new_order={
            "symbol": "035420",
            "side": "buy",
            "value_krw": Decimal("1000000"),
        },
    )
    assert impact.after.total_exposure_krw > impact.before.total_exposure_krw
    assert impact.after.symbol_count == impact.before.symbol_count + 1
    print("✅ test_project_buy_increases_exposure")


def test_project_sell_decreases_exposure():
    """매도 후 노출 감소."""
    a = _analyzer()
    pos = {
        "005930": {"market_value_krw": Decimal("2000000")},
    }
    impact = a.project(
        current_positions=pos,
        equity_krw=Decimal("10000000"),
        new_order={
            "symbol": "005930",
            "side": "sell",
            "value_krw": Decimal("1000000"),
        },
    )
    assert impact.after.total_exposure_krw < impact.before.total_exposure_krw
    print("✅ test_project_sell_decreases_exposure")


def test_project_full_sell_removes_position():
    """전량 매도 → 포지션 사라짐."""
    a = _analyzer()
    pos = {
        "005930": {"market_value_krw": Decimal("1000000")},
    }
    impact = a.project(
        current_positions=pos,
        equity_krw=Decimal("10000000"),
        new_order={
            "symbol": "005930",
            "side": "sell",
            "value_krw": Decimal("1000000"),
        },
    )
    assert impact.after.symbol_count == 0
    print("✅ test_project_full_sell_removes_position")


def test_project_would_exceed_total():
    """매수 후 전체 노출 초과 감지."""
    a = _analyzer(max_total_exposure_pct=Decimal("0.30"))
    pos = {
        "005930": {"market_value_krw": Decimal("2000000")},  # 20%
    }
    impact = a.project(
        current_positions=pos,
        equity_krw=Decimal("10000000"),
        new_order={
            "symbol": "035420",
            "side": "buy",
            "value_krw": Decimal("2000000"),  # +20% → 40% > 30%
        },
    )
    assert impact.would_exceed.get("total_exposure") is True
    print("✅ test_project_would_exceed_total")


def test_project_invalid_order():
    """잘못된 주문 → 변화 없음."""
    a = _analyzer()
    pos = {"005930": {"market_value_krw": Decimal("1000000")}}
    impact = a.project(
        current_positions=pos,
        equity_krw=Decimal("10000000"),
        new_order={"symbol": "", "side": "buy", "value_krw": 0},
    )
    assert impact.before.symbol_count == impact.after.symbol_count
    assert "invalid" in impact.note.lower()
    print("✅ test_project_invalid_order")


# ─────────────────────────────────────────────────
# 직렬화 (Serialization)
# ─────────────────────────────────────────────────

def test_snapshot_to_dict():
    import json
    a = _analyzer()
    pos = {"005930": {"market_value_krw": Decimal("1000000")}}
    snap = a.analyze(positions=pos, equity_krw=Decimal("10000000"))
    d = snap.to_dict()
    s = json.dumps(d)  # 모두 직렬화 가능해야
    assert "005930" in s
    assert "severity" in d
    print("✅ test_snapshot_to_dict")


def test_impact_to_dict():
    import json
    a = _analyzer()
    pos = {"005930": {"market_value_krw": Decimal("1000000")}}
    impact = a.project(
        current_positions=pos,
        equity_krw=Decimal("10000000"),
        new_order={"symbol": "000660", "side": "buy", "value_krw": Decimal("500000")},
    )
    d = impact.to_dict()
    s = json.dumps(d)
    assert "before" in d
    assert "after" in d
    print("✅ test_impact_to_dict")


# ─────────────────────────────────────────────────
# 불변성 (Immutability)
# ─────────────────────────────────────────────────

def test_snapshot_immutable():
    a = _analyzer()
    pos = {"005930": {"market_value_krw": Decimal("1000000")}}
    snap = a.analyze(positions=pos, equity_krw=Decimal("10000000"))
    try:
        snap.equity_krw = Decimal(0)  # type: ignore[misc]
        assert False
    except Exception:
        pass
    print("✅ test_snapshot_immutable")


# ─────────────────────────────────────────────────
# 편의 함수 (Convenience)
# ─────────────────────────────────────────────────

def test_quick_analyze():
    pos = {"005930": {"market_value_krw": Decimal("1000000")}}
    snap = quick_analyze(
        positions=pos,
        equity_krw=Decimal("10000000"),
        sector_map={"005930": "tech"},
    )
    assert snap.symbol_count == 1
    print("✅ test_quick_analyze")


# ─────────────────────────────────────────────────
# 실행
# ─────────────────────────────────────────────────

def _run_all() -> int:
    failed = 0
    tests = [
        test_config_defaults,
        test_config_invalid_pct,
        test_config_invalid_diversification,
        test_empty_positions,
        test_single_position_under_limits,
        test_total_exposure_warning,
        test_single_symbol_warning,
        test_sector_concentration_warning,
        test_etf_exempt_from_sector,
        test_etf_not_exempt_when_disabled,
        test_diversification_warning,
        test_unknown_sector_handling,
        test_hhi_concentrated,
        test_hhi_diversified,
        test_zero_equity_with_exposure,
        test_zero_market_value_skipped,
        test_invalid_market_value_treated_as_zero,
        test_project_buy_increases_exposure,
        test_project_sell_decreases_exposure,
        test_project_full_sell_removes_position,
        test_project_would_exceed_total,
        test_project_invalid_order,
        test_snapshot_to_dict,
        test_impact_to_dict,
        test_snapshot_immutable,
        test_quick_analyze,
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
    print("Task 47 v0.2 — portfolio_risk 스모크 테스트")
    print("─" * 50)
    failed = _run_all()
    print("─" * 50)
    if failed == 0:
        print("✅ 모든 테스트 통과")
        sys.exit(0)
    else:
        print(f"❌ {failed}개 실패")
        sys.exit(1)
