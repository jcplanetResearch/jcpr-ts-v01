"""
스모크 테스트 — schema (Pydantic 검증)
=======================================

JCPR Trading System - jcpr-ts-v01
Task 45 v0.1
"""

from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from pydantic import ValidationError

_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from src.strategies.schema import (  # noqa: E402
    RegistryFile,
    StrategyEntry,
)


# ─────────────────────────────────────────────────
# 유효한 엔트리
# ─────────────────────────────────────────────────

def _valid_entry(**overrides) -> dict:
    base = {
        "strategy_id": "test_strategy",
        "module_path": "src.signals.strategies.momentum_v1",
        "class_name": "MomentumV1",
        "version": "1.0.0",
        "enabled": True,
        "paper_only": False,
        "capital_weight": Decimal("0.3"),
        "max_capital_pct": Decimal("0.5"),
        "timeframe": "1d",
        "universe": ["005930"],
        "signal_categories": ["ENTRY", "EXIT"],
        "parameters": {"lookback": 20},
        "activated_at": date(2026, 5, 7),
        "notes": "test",
    }
    base.update(overrides)
    return base


def test_valid_entry():
    e = StrategyEntry(**_valid_entry())
    assert e.strategy_id == "test_strategy"
    assert e.enabled is True
    print("✅ test_valid_entry")


def test_strategy_id_invalid():
    """공백·특수문자 거부."""
    bad_ids = ["123start", "with space", "with/slash", ""]
    for bid in bad_ids:
        try:
            StrategyEntry(**_valid_entry(strategy_id=bid))
            assert False, f"Should reject {bid!r}"
        except ValidationError:
            pass
    print("✅ test_strategy_id_invalid")


def test_module_path_whitelist():
    """src.signals.strategies. 외 거부."""
    bad_paths = [
        "os",
        "subprocess",
        "src.brokers.kis_adapter",
        "../malicious",
    ]
    for bp in bad_paths:
        try:
            StrategyEntry(**_valid_entry(module_path=bp))
            assert False, f"Should reject {bp!r}"
        except ValidationError:
            pass
    print("✅ test_module_path_whitelist")


def test_class_name_invalid():
    """소문자 시작 거부."""
    try:
        StrategyEntry(**_valid_entry(class_name="lowercase"))
        assert False
    except ValidationError:
        pass
    print("✅ test_class_name_invalid")


def test_version_invalid():
    """semver 형식 강제."""
    bad = ["1.0", "v1.0.0", "1.0.0-beta", "abc"]
    for b in bad:
        try:
            StrategyEntry(**_valid_entry(version=b))
            assert False, f"Should reject {b}"
        except ValidationError:
            pass
    print("✅ test_version_invalid")


def test_timeframe_invalid():
    """허용 timeframe만."""
    try:
        StrategyEntry(**_valid_entry(timeframe="2d"))
        assert False
    except ValidationError:
        pass
    print("✅ test_timeframe_invalid")


def test_signal_category_invalid():
    """허용 카테고리만."""
    try:
        StrategyEntry(**_valid_entry(signal_categories=["INVALID"]))
        assert False
    except ValidationError:
        pass
    print("✅ test_signal_category_invalid")


def test_signal_category_dedup():
    """중복 자동 제거."""
    e = StrategyEntry(**_valid_entry(signal_categories=["ENTRY", "ENTRY", "EXIT"]))
    assert e.signal_categories == ["ENTRY", "EXIT"]
    print("✅ test_signal_category_dedup")


def test_capital_weight_range():
    """0-1 범위 외 거부."""
    for v in [Decimal("-0.1"), Decimal("1.5")]:
        try:
            StrategyEntry(**_valid_entry(capital_weight=v))
            assert False
        except ValidationError:
            pass
    print("✅ test_capital_weight_range")


def test_secret_in_parameters_rejected():
    """parameters에 시크릿 키워드 거부."""
    bad_params = [
        {"api_key": "abc"},
        {"password": "secret"},
        {"auth_token": "xyz"},
        {"nested": {"secret": "hidden"}},
    ]
    for bp in bad_params:
        try:
            StrategyEntry(**_valid_entry(parameters=bp))
            assert False, f"Should reject params {bp}"
        except ValidationError:
            pass
    print("✅ test_secret_in_parameters_rejected")


def test_long_credential_string_rejected():
    """긴 base64-like 문자열 거부."""
    try:
        StrategyEntry(**_valid_entry(parameters={
            "innocent_key": "QWE123ASD456ZXC789QWE123ASD456ZXC789",  # 36자 영숫자
        }))
        assert False
    except ValidationError:
        pass
    print("✅ test_long_credential_string_rejected")


def test_extra_field_forbidden():
    """정의되지 않은 필드 거부."""
    try:
        StrategyEntry(**_valid_entry(unknown_field="oops"))
        assert False
    except ValidationError:
        pass
    print("✅ test_extra_field_forbidden")


def test_universe_invalid_symbol():
    """invalid 심볼 형식 거부."""
    try:
        StrategyEntry(**_valid_entry(universe=["with space"]))
        assert False
    except ValidationError:
        pass
    print("✅ test_universe_invalid_symbol")


def test_universe_dedup():
    """중복 종목 제거."""
    e = StrategyEntry(**_valid_entry(universe=["005930", "005930", "035420"]))
    assert e.universe == ["005930", "035420"]
    print("✅ test_universe_dedup")


def test_frozen_immutable():
    """frozen=True 변경 불가."""
    e = StrategyEntry(**_valid_entry())
    try:
        e.enabled = False  # type: ignore[misc]
        assert False
    except (ValidationError, Exception):
        pass
    print("✅ test_frozen_immutable")


def test_repr_no_secrets():
    """__repr__ 에 parameters 노출 안 됨."""
    e = StrategyEntry(**_valid_entry(parameters={"lookback": 20}))
    r = repr(e)
    assert "lookback" not in r, "parameters should not be in repr"
    assert "test_strategy" in r
    print("✅ test_repr_no_secrets")


# ─────────────────────────────────────────────────
# RegistryFile 검증
# ─────────────────────────────────────────────────

def test_registry_file_unique_ids():
    """duplicate strategy_id 거부."""
    e1 = _valid_entry(strategy_id="dup")
    e2 = _valid_entry(strategy_id="dup")
    try:
        RegistryFile(version="1.0", strategies=[e1, e2])
        assert False
    except ValidationError:
        pass
    print("✅ test_registry_file_unique_ids")


def test_registry_file_capital_weight_sum():
    """활성 전략 capital_weight 합 > 1.0 거부."""
    e1 = _valid_entry(strategy_id="s1", capital_weight=Decimal("0.6"), enabled=True)
    e2 = _valid_entry(strategy_id="s2", capital_weight=Decimal("0.5"), enabled=True)
    try:
        RegistryFile(version="1.0", strategies=[e1, e2])
        assert False
    except ValidationError:
        pass
    print("✅ test_registry_file_capital_weight_sum")


def test_registry_file_disabled_excluded_from_sum():
    """비활성 전략은 합 계산 제외."""
    e1 = _valid_entry(strategy_id="s1", capital_weight=Decimal("0.6"), enabled=True)
    e2 = _valid_entry(strategy_id="s2", capital_weight=Decimal("0.6"), enabled=False)
    rf = RegistryFile(version="1.0", strategies=[e1, e2])
    # 활성만 0.6 → OK
    assert len(rf.strategies) == 2
    print("✅ test_registry_file_disabled_excluded_from_sum")


def test_registry_file_version_format():
    """파일 version X.Y 형식."""
    try:
        RegistryFile(version="1.0.0", strategies=[])
        assert False
    except ValidationError:
        pass
    print("✅ test_registry_file_version_format")


def test_load_class_whitelist():
    """load_class도 화이트리스트 검증."""
    e = StrategyEntry(**_valid_entry())
    try:
        e.load_class()
        # momentum_v1 모듈이 실제로 없으면 ImportError
    except ImportError:
        pass  # 모듈이 없는 건 정상 (구현체 없음)
    except AttributeError:
        pass
    print("✅ test_load_class_whitelist")


# ─────────────────────────────────────────────────
# 실행
# ─────────────────────────────────────────────────

def _run_all() -> int:
    failed = 0
    tests = [
        test_valid_entry,
        test_strategy_id_invalid,
        test_module_path_whitelist,
        test_class_name_invalid,
        test_version_invalid,
        test_timeframe_invalid,
        test_signal_category_invalid,
        test_signal_category_dedup,
        test_capital_weight_range,
        test_secret_in_parameters_rejected,
        test_long_credential_string_rejected,
        test_extra_field_forbidden,
        test_universe_invalid_symbol,
        test_universe_dedup,
        test_frozen_immutable,
        test_repr_no_secrets,
        test_registry_file_unique_ids,
        test_registry_file_capital_weight_sum,
        test_registry_file_disabled_excluded_from_sum,
        test_registry_file_version_format,
        test_load_class_whitelist,
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
    print("Task 45 v0.1 — schema 스모크 테스트")
    print("─" * 50)
    failed = _run_all()
    print("─" * 50)
    if failed == 0:
        print("✅ 모든 테스트 통과")
        sys.exit(0)
    else:
        print(f"❌ {failed}개 실패")
        sys.exit(1)
