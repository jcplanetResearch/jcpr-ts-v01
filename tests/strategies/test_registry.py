"""
스모크 테스트 — registry (StrategyRegistry)
============================================

JCPR Trading System - jcpr-ts-v01
Task 45 v0.1
"""

from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from src.strategies.registry import StrategyRegistry  # noqa: E402
from src.strategies.schema import RegistryFile, StrategyEntry  # noqa: E402


def _make_entry(
    strategy_id: str,
    *,
    enabled: bool = True,
    paper_only: bool = False,
    timeframe: str = "1d",
    universe: list[str] | None = None,
    capital_weight: str = "0.0",
) -> StrategyEntry:
    return StrategyEntry(
        strategy_id=strategy_id,
        module_path="src.signals.strategies.momentum_v1",
        class_name="MomentumV1",
        version="1.0.0",
        enabled=enabled,
        paper_only=paper_only,
        capital_weight=Decimal(capital_weight),
        max_capital_pct=Decimal("0.5"),
        timeframe=timeframe,
        universe=universe or [],
        signal_categories=["ENTRY", "EXIT"],
        parameters={},
    )


def _make_registry(*entries: StrategyEntry) -> StrategyRegistry:
    rf = RegistryFile(version="1.0", strategies=list(entries))
    return StrategyRegistry.from_registry_file(rf)


def test_empty_registry():
    r = _make_registry()
    assert len(r) == 0
    assert r.list_active() == ()
    assert r.total_capital_weight() == Decimal(0)
    print("✅ test_empty_registry")


def test_get_and_require():
    r = _make_registry(_make_entry("s1"), _make_entry("s2"))
    assert r.get("s1") is not None
    assert r.get("nonexistent") is None
    assert r.require("s1").strategy_id == "s1"
    try:
        r.require("nonexistent")
        assert False
    except KeyError:
        pass
    print("✅ test_get_and_require")


def test_contains():
    r = _make_registry(_make_entry("s1"))
    assert "s1" in r
    assert "s2" not in r
    print("✅ test_contains")


def test_list_active():
    r = _make_registry(
        _make_entry("active1", enabled=True),
        _make_entry("disabled1", enabled=False),
        _make_entry("active2", enabled=True),
    )
    active = r.list_active()
    ids = sorted(e.strategy_id for e in active)
    assert ids == ["active1", "active2"]
    print("✅ test_list_active")


def test_list_paper_only():
    r = _make_registry(
        _make_entry("paper1", paper_only=True),
        _make_entry("live1", paper_only=False),
    )
    paper = r.list_paper_only()
    assert len(paper) == 1
    assert paper[0].strategy_id == "paper1"
    print("✅ test_list_paper_only")


def test_list_live_eligible():
    r = _make_registry(
        _make_entry("eligible", enabled=True, paper_only=False),
        _make_entry("paper_disabled", enabled=False, paper_only=True),
        _make_entry("paper_enabled", enabled=True, paper_only=True),
        _make_entry("live_disabled", enabled=False, paper_only=False),
    )
    eligible = r.list_live_eligible()
    assert len(eligible) == 1
    assert eligible[0].strategy_id == "eligible"
    print("✅ test_list_live_eligible")


def test_list_by_timeframe():
    r = _make_registry(
        _make_entry("daily1", timeframe="1d"),
        _make_entry("daily2", timeframe="1d"),
        _make_entry("hourly", timeframe="1h"),
    )
    daily = r.list_by_timeframe("1d")
    assert len(daily) == 2
    hourly = r.list_by_timeframe("1h")
    assert len(hourly) == 1
    print("✅ test_list_by_timeframe")


def test_list_by_symbol():
    r = _make_registry(
        _make_entry("specific", universe=["005930"]),
        _make_entry("all_symbols", universe=[]),  # 전체 허용
        _make_entry("other", universe=["035420"]),
    )
    matched = r.list_by_symbol("005930")
    ids = sorted(e.strategy_id for e in matched)
    assert ids == ["all_symbols", "specific"]
    print("✅ test_list_by_symbol")


def test_total_capital_weight():
    r = _make_registry(
        _make_entry("s1", enabled=True, capital_weight="0.3"),
        _make_entry("s2", enabled=True, capital_weight="0.2"),
        _make_entry("s3", enabled=False, capital_weight="0.5"),  # 제외
    )
    assert r.total_capital_weight(active_only=True) == Decimal("0.5")
    assert r.total_capital_weight(active_only=False) == Decimal("1.0")
    print("✅ test_total_capital_weight")


def test_is_paper_only_unknown_safe():
    """미등록 ID는 True (안전 default)."""
    r = _make_registry(_make_entry("s1", paper_only=False))
    assert r.is_paper_only("s1") is False
    assert r.is_paper_only("unknown") is True  # 안전 default
    print("✅ test_is_paper_only_unknown_safe")


def test_summary_serializable():
    """summary가 JSON 직렬화 가능."""
    import json
    r = _make_registry(
        _make_entry("s1", enabled=True, capital_weight="0.3"),
        _make_entry("s2", paper_only=True),
    )
    s = r.summary()
    j = json.dumps(s, default=str)  # Decimal 등 fallback
    assert "s1" in j
    assert "total_strategies" in s
    assert s["active_count"] == 2
    print("✅ test_summary_serializable")


def test_iteration():
    """__iter__ 작동."""
    r = _make_registry(_make_entry("s1"), _make_entry("s2"))
    ids = sorted(e.strategy_id for e in r)
    assert ids == ["s1", "s2"]
    print("✅ test_iteration")


def test_registry_immutable():
    """frozen=True."""
    r = _make_registry(_make_entry("s1"))
    try:
        r.entries = ()  # type: ignore[misc]
        assert False
    except Exception:
        pass
    print("✅ test_registry_immutable")


def _run_all() -> int:
    failed = 0
    tests = [
        test_empty_registry,
        test_get_and_require,
        test_contains,
        test_list_active,
        test_list_paper_only,
        test_list_live_eligible,
        test_list_by_timeframe,
        test_list_by_symbol,
        test_total_capital_weight,
        test_is_paper_only_unknown_safe,
        test_summary_serializable,
        test_iteration,
        test_registry_immutable,
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
    print("Task 45 v0.1 — registry 스모크 테스트")
    print("─" * 50)
    failed = _run_all()
    print("─" * 50)
    if failed == 0:
        print("✅ 모든 테스트 통과")
        sys.exit(0)
    else:
        print(f"❌ {failed}개 실패")
        sys.exit(1)
