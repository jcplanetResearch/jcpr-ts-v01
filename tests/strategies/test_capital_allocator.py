"""
스모크 테스트 — capital_allocator
==================================

JCPR Trading System - jcpr-ts-v01
Task 46 v0.1
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from src.strategies.capital_allocator import (  # noqa: E402
    CapitalAllocation,
    EPSILON,
    MODE_LIVE,
    MODE_PAPER,
    StrategyAllocation,
    allocate_capital,
)
from src.strategies.registry import StrategyRegistry  # noqa: E402
from src.strategies.schema import RegistryFile, StrategyEntry  # noqa: E402


# ─────────────────────────────────────────────────
# 헬퍼 (Helpers)
# ─────────────────────────────────────────────────

def _entry(
    strategy_id: str,
    *,
    enabled: bool = True,
    paper_only: bool = False,
    capital_weight: str = "0.0",
    max_capital_pct: str = "1.0",
) -> StrategyEntry:
    return StrategyEntry(
        strategy_id=strategy_id,
        module_path="src.signals.strategies.x",
        class_name="X",
        version="1.0.0",
        enabled=enabled,
        paper_only=paper_only,
        capital_weight=Decimal(capital_weight),
        max_capital_pct=Decimal(max_capital_pct),
        timeframe="1d",
    )


def _registry(*entries: StrategyEntry) -> StrategyRegistry:
    rf = RegistryFile(version="1.0", strategies=list(entries))
    return StrategyRegistry.from_registry_file(rf)


# ─────────────────────────────────────────────────
# 기본 시나리오 (Basic Scenarios)
# ─────────────────────────────────────────────────

def test_empty_registry():
    """빈 레지스트리 — 전체가 현금."""
    r = _registry()
    a = allocate_capital(r, Decimal("10000000"))
    assert a.allocated_total_krw == Decimal(0)
    assert a.cash_buffer_krw == Decimal("10000000")
    assert a.cash_buffer_pct == Decimal("1.0000")
    assert len(a.allocations) == 0
    assert any("활성 전략 없음" in w for w in a.warnings)
    print("✅ test_empty_registry")


def test_zero_capital():
    """자본 0 — 결과 모두 0."""
    r = _registry(_entry("s1", capital_weight="0.5", max_capital_pct="1.0"))
    a = allocate_capital(r, Decimal(0))
    assert a.total_capital_krw == Decimal(0)
    assert a.allocated_total_krw == Decimal(0)
    assert a.cash_buffer_krw == Decimal(0)
    print("✅ test_zero_capital")


def test_negative_capital_rejected():
    r = _registry()
    try:
        allocate_capital(r, Decimal("-1"))
        assert False, "Should reject negative capital"
    except ValueError:
        pass
    print("✅ test_negative_capital_rejected")


def test_invalid_mode_rejected():
    r = _registry()
    try:
        allocate_capital(r, Decimal("1000"), mode="invalid")
        assert False
    except ValueError:
        pass
    print("✅ test_invalid_mode_rejected")


# ─────────────────────────────────────────────────
# 비례 분배 (Proportional)
# ─────────────────────────────────────────────────

def test_simple_proportional_no_cap():
    """단순 비례 분배 — 캡 안 걸림."""
    r = _registry(
        _entry("s1", capital_weight="0.4", max_capital_pct="0.5"),
        _entry("s2", capital_weight="0.3", max_capital_pct="0.5"),
    )
    a = allocate_capital(r, Decimal("10000000"))
    s1 = a.get("s1")
    s2 = a.get("s2")
    assert s1.allocated_krw == Decimal("4000000")
    assert s2.allocated_krw == Decimal("3000000")
    assert not s1.capped
    assert not s2.capped
    # 0.4 + 0.3 = 0.7 → 30% 현금
    assert a.cash_buffer_krw == Decimal("3000000")
    print("✅ test_simple_proportional_no_cap")


def test_full_allocation():
    """가중치 합 1.0 — 현금 버퍼 0."""
    r = _registry(
        _entry("s1", capital_weight="0.6", max_capital_pct="1.0"),
        _entry("s2", capital_weight="0.4", max_capital_pct="1.0"),
    )
    a = allocate_capital(r, Decimal("10000000"))
    assert a.allocated_total_krw == Decimal("10000000")
    assert a.cash_buffer_krw == Decimal(0)
    print("✅ test_full_allocation")


# ─────────────────────────────────────────────────
# 캡 + 재분배 (Cap & Redistribute)
# ─────────────────────────────────────────────────

def test_cap_applied_no_redistribute():
    """redistribute_overflow=False — 초과분은 현금."""
    r = _registry(
        _entry("s1", capital_weight="0.8", max_capital_pct="0.5"),
    )
    a = allocate_capital(
        r, Decimal("10000000"),
        redistribute_overflow=False,
    )
    s1 = a.get("s1")
    # 0.8 * 10M = 8M → 캡 5M 적용
    assert s1.allocated_krw == Decimal("5000000")
    assert s1.capped
    # 8M-5M=3M 초과 → 현금으로
    assert a.cash_buffer_krw == Decimal("5000000")
    assert any("캡 초과분" in w for w in a.warnings)
    print("✅ test_cap_applied_no_redistribute")


def test_cap_with_redistribute():
    """redistribute_overflow=True — 초과분 다른 전략에 재분배."""
    r = _registry(
        _entry("s1", capital_weight="0.8", max_capital_pct="0.5"),  # 캡 5M
        _entry("s2", capital_weight="0.2", max_capital_pct="1.0"),  # 캡 10M
    )
    a = allocate_capital(
        r, Decimal("10000000"),
        redistribute_overflow=True,
    )
    s1 = a.get("s1")
    s2 = a.get("s2")
    # s1: 비례 8M → 캡 5M, 초과 3M
    # s2: 비례 2M + 재분배 3M = 5M (s2 캡 10M 안 걸림)
    assert s1.allocated_krw == Decimal("5000000")
    assert s1.capped
    assert s2.allocated_krw == Decimal("5000000")
    assert not s2.capped
    assert s2.iterations_after_cap >= 1
    # 합 10M, 현금 0
    assert a.allocated_total_krw == Decimal("10000000")
    assert a.cash_buffer_krw == Decimal(0)
    print("✅ test_cap_with_redistribute")


def test_redistribute_all_capped():
    """모든 전략이 캡 도달 시 잔여는 현금."""
    r = _registry(
        _entry("s1", capital_weight="0.5", max_capital_pct="0.3"),  # 캡 3M
        _entry("s2", capital_weight="0.5", max_capital_pct="0.3"),  # 캡 3M
    )
    a = allocate_capital(
        r, Decimal("10000000"),
        redistribute_overflow=True,
    )
    s1 = a.get("s1")
    s2 = a.get("s2")
    assert s1.allocated_krw == Decimal("3000000")
    assert s2.allocated_krw == Decimal("3000000")
    assert s1.capped and s2.capped
    # 비례: 5M+5M=10M, 캡: 3M+3M=6M, 잔여 4M는 현금
    assert a.cash_buffer_krw == Decimal("4000000")
    assert any("재분배 불가" in w for w in a.warnings)
    print("✅ test_redistribute_all_capped")


def test_redistribute_iterative():
    """재분배 반복: 한 번 재분배 후 다른 전략도 캡 도달 시 재재분배."""
    r = _registry(
        _entry("s1", capital_weight="0.6", max_capital_pct="0.4"),  # 캡 4M
        _entry("s2", capital_weight="0.3", max_capital_pct="0.4"),  # 캡 4M
        _entry("s3", capital_weight="0.1", max_capital_pct="1.0"),  # 캡 10M
    )
    a = allocate_capital(
        r, Decimal("10000000"),
        redistribute_overflow=True,
    )
    # s1: 비례 6M → 캡 4M, 초과 2M
    # s2: 비례 3M → 4M 미만, 재분배 후 더 받을 수 있음
    # s3: 비례 1M, 재분배 가능
    s1 = a.get("s1")
    s2 = a.get("s2")
    s3 = a.get("s3")
    assert s1.allocated_krw == Decimal("4000000")
    assert s1.capped
    # 합은 10M (모두 분배됨)
    assert a.allocated_total_krw == Decimal("10000000")
    print("✅ test_redistribute_iterative")


# ─────────────────────────────────────────────────
# 모드 (Mode)
# ─────────────────────────────────────────────────

def test_live_mode_excludes_paper_only():
    """live 모드 — paper_only 전략 제외."""
    r = _registry(
        _entry("live1", paper_only=False, capital_weight="0.4"),
        _entry("paper1", paper_only=True, capital_weight="0.4"),
    )
    a = allocate_capital(r, Decimal("10000000"), mode=MODE_LIVE)
    assert a.get("live1") is not None
    assert a.get("paper1") is None
    assert "paper1" in a.excluded_strategies
    print("✅ test_live_mode_excludes_paper_only")


def test_paper_mode_includes_paper_only():
    """paper 모드 — paper_only도 포함."""
    r = _registry(
        _entry("live1", paper_only=False, capital_weight="0.4"),
        _entry("paper1", paper_only=True, capital_weight="0.4"),
    )
    a = allocate_capital(r, Decimal("10000000"), mode=MODE_PAPER)
    assert a.get("live1") is not None
    assert a.get("paper1") is not None
    print("✅ test_paper_mode_includes_paper_only")


def test_disabled_excluded():
    """enabled=False 전략은 어떤 모드에서도 제외."""
    r = _registry(
        _entry("active", enabled=True, capital_weight="0.5"),
        _entry("disabled", enabled=False, capital_weight="0.5"),
    )
    a_live = allocate_capital(r, Decimal("10000000"), mode=MODE_LIVE)
    a_paper = allocate_capital(r, Decimal("10000000"), mode=MODE_PAPER)
    assert a_live.get("disabled") is None
    assert a_paper.get("disabled") is None
    print("✅ test_disabled_excluded")


# ─────────────────────────────────────────────────
# 직렬화 (Serialization)
# ─────────────────────────────────────────────────

def test_to_dict_serializable():
    import json
    r = _registry(_entry("s1", capital_weight="0.5", max_capital_pct="1.0"))
    a = allocate_capital(r, Decimal("10000000"))
    d = a.to_dict()
    j = json.dumps(d)  # 모두 직렬화 가능해야
    assert "s1" in j
    assert "allocated_krw" in j
    print("✅ test_to_dict_serializable")


def test_to_json_valid():
    import json
    r = _registry(_entry("s1", capital_weight="0.5"))
    a = allocate_capital(r, Decimal("10000000"))
    s = a.to_json()
    parsed = json.loads(s)
    assert parsed["mode"] == "live"
    print("✅ test_to_json_valid")


# ─────────────────────────────────────────────────
# 불변성 (Immutability)
# ─────────────────────────────────────────────────

def test_allocation_immutable():
    r = _registry(_entry("s1", capital_weight="0.5"))
    a = allocate_capital(r, Decimal("10000000"))
    try:
        a.total_capital_krw = Decimal(0)  # type: ignore[misc]
        assert False
    except Exception:
        pass
    print("✅ test_allocation_immutable")


def test_strategy_allocation_immutable():
    r = _registry(_entry("s1", capital_weight="0.5"))
    a = allocate_capital(r, Decimal("10000000"))
    s1 = a.get("s1")
    try:
        s1.allocated_krw = Decimal(0)  # type: ignore[misc]
        assert False
    except Exception:
        pass
    print("✅ test_strategy_allocation_immutable")


# ─────────────────────────────────────────────────
# 정밀도 (Precision)
# ─────────────────────────────────────────────────

def test_decimal_precision():
    """Decimal 정밀도 — float 오차 없음."""
    r = _registry(
        _entry("s1", capital_weight="0.333", max_capital_pct="1.0"),
        _entry("s2", capital_weight="0.667", max_capital_pct="1.0"),
    )
    a = allocate_capital(r, Decimal("10000000"))
    s1 = a.get("s1")
    s2 = a.get("s2")
    # 0.333 * 10M = 3,330,000
    # 0.667 * 10M = 6,670,000
    assert s1.allocated_krw == Decimal("3330000")
    assert s2.allocated_krw == Decimal("6670000")
    print("✅ test_decimal_precision")


def test_quantize_to_krw_integer():
    """KRW는 정수 단위 quantize."""
    r = _registry(
        _entry("s1", capital_weight="0.333333", max_capital_pct="1.0"),
    )
    a = allocate_capital(r, Decimal("1000000"))
    s1 = a.get("s1")
    # 0.333333 * 1M = 333333.0 → 정수
    assert s1.allocated_krw == s1.allocated_krw.to_integral_value()
    print("✅ test_quantize_to_krw_integer")


# ─────────────────────────────────────────────────
# 통합: 현실적 시나리오
# ─────────────────────────────────────────────────

def test_realistic_3_strategies():
    """현실적 시나리오 — example YAML 형태."""
    r = _registry(
        _entry("momentum_v1", paper_only=False,
               capital_weight="0.6", max_capital_pct="0.3"),  # 캡 발동
        _entry("mean_rev_v1", enabled=False,
               capital_weight="0.0", max_capital_pct="0.1"),  # 비활성
        _entry("stop_loss_v1", paper_only=False,
               capital_weight="0.0", max_capital_pct="0.0"),
    )
    a = allocate_capital(r, Decimal("100000000"))  # 1억
    # momentum_v1: 0.6*1억 = 6천만 → 캡 0.3*1억 = 3천만 → 캡 적용
    # 재분배: 가중치 0인 stop_loss는 받기 어려움
    mom = a.get("momentum_v1")
    assert mom.allocated_krw == Decimal("30000000")
    assert mom.capped
    # mean_rev는 비활성 → 제외
    assert a.get("mean_rev_v1") is None
    assert "mean_rev_v1" in a.excluded_strategies
    print("✅ test_realistic_3_strategies")


# ─────────────────────────────────────────────────
# 실행
# ─────────────────────────────────────────────────

def _run_all() -> int:
    failed = 0
    tests = [
        test_empty_registry,
        test_zero_capital,
        test_negative_capital_rejected,
        test_invalid_mode_rejected,
        test_simple_proportional_no_cap,
        test_full_allocation,
        test_cap_applied_no_redistribute,
        test_cap_with_redistribute,
        test_redistribute_all_capped,
        test_redistribute_iterative,
        test_live_mode_excludes_paper_only,
        test_paper_mode_includes_paper_only,
        test_disabled_excluded,
        test_to_dict_serializable,
        test_to_json_valid,
        test_allocation_immutable,
        test_strategy_allocation_immutable,
        test_decimal_precision,
        test_quantize_to_krw_integer,
        test_realistic_3_strategies,
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
    print("Task 46 v0.1 — capital_allocator 스모크 테스트")
    print("─" * 50)
    failed = _run_all()
    print("─" * 50)
    if failed == 0:
        print("✅ 모든 테스트 통과")
        sys.exit(0)
    else:
        print(f"❌ {failed}개 실패")
        sys.exit(1)
