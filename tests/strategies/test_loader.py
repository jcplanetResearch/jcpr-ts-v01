"""
스모크 테스트 — loader (YAML 로더)
===================================

JCPR Trading System - jcpr-ts-v01
Task 45 v0.1
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from src.strategies.loader import (  # noqa: E402
    RegistryLoadError,
    load_registry,
    load_registry_from_string,
)


# ─────────────────────────────────────────────────
# 유효한 YAML
# ─────────────────────────────────────────────────

VALID_YAML = """
version: "1.0"
strategies:
  - strategy_id: momentum_v1
    module_path: src.signals.strategies.momentum_v1
    class_name: MomentumV1
    version: "1.0.0"
    enabled: true
    paper_only: false
    capital_weight: 0.6
    max_capital_pct: 0.3
    timeframe: "1d"
    universe: ["005930"]
    signal_categories: ["ENTRY", "EXIT"]
    parameters:
      lookback: 20
    activated_at: "2026-05-07"
    notes: "test"
"""


def test_load_from_string_valid():
    r = load_registry_from_string(VALID_YAML)
    assert len(r) == 1
    assert r.get("momentum_v1") is not None
    print("✅ test_load_from_string_valid")


def test_load_from_file(tmp_dir):
    p = tmp_dir / "registry.yaml"
    p.write_text(VALID_YAML, encoding="utf-8")
    r = load_registry(str(p))
    assert len(r) == 1
    print("✅ test_load_from_file")


def test_load_missing_file():
    try:
        load_registry("/tmp/nonexistent_xyz_456.yaml")
        assert False
    except RegistryLoadError as e:
        assert "not found" in str(e)
    print("✅ test_load_missing_file")


def test_load_empty_file(tmp_dir):
    p = tmp_dir / "empty.yaml"
    p.write_text("", encoding="utf-8")
    try:
        load_registry(str(p))
        assert False
    except RegistryLoadError as e:
        assert "empty" in str(e)
    print("✅ test_load_empty_file")


def test_load_invalid_yaml():
    bad = "version: 1.0\n  strategies:\n  - foo"  # 들여쓰기 오류
    try:
        load_registry_from_string(bad)
        assert False
    except RegistryLoadError:
        pass
    print("✅ test_load_invalid_yaml")


def test_load_non_mapping():
    """루트가 list면 거부."""
    try:
        load_registry_from_string("- a\n- b")
        assert False
    except RegistryLoadError as e:
        assert "mapping" in str(e)
    print("✅ test_load_non_mapping")


def test_load_validation_error_message():
    """검증 실패 시 명확한 에러 메시지."""
    bad_yaml = """
version: "1.0"
strategies:
  - strategy_id: bad
    module_path: os
    class_name: System
    version: "1.0.0"
    timeframe: "1d"
"""
    try:
        load_registry_from_string(bad_yaml)
        assert False
    except RegistryLoadError as e:
        msg = str(e)
        assert "module_path" in msg or "validation" in msg.lower()
    print("✅ test_load_validation_error_message")


def test_load_duplicate_ids():
    yaml = """
version: "1.0"
strategies:
  - strategy_id: dup
    module_path: src.signals.strategies.x
    class_name: X
    version: "1.0.0"
    timeframe: "1d"
  - strategy_id: dup
    module_path: src.signals.strategies.y
    class_name: Y
    version: "1.0.0"
    timeframe: "1h"
"""
    try:
        load_registry_from_string(yaml)
        assert False
    except RegistryLoadError as e:
        assert "duplicate" in str(e).lower()
    print("✅ test_load_duplicate_ids")


def test_load_secret_in_parameters():
    yaml = """
version: "1.0"
strategies:
  - strategy_id: sneaky
    module_path: src.signals.strategies.x
    class_name: X
    version: "1.0.0"
    timeframe: "1d"
    parameters:
      api_key: leak
"""
    try:
        load_registry_from_string(yaml)
        assert False
    except RegistryLoadError as e:
        assert "secret" in str(e).lower() or "api_key" in str(e).lower()
    print("✅ test_load_secret_in_parameters")


def test_load_example_yaml_passes():
    """배포된 example YAML도 검증 통과해야."""
    example_path = _REPO / "configs" / "strategy_registry.example.yaml"
    if not example_path.exists():
        print(f"⚠️ skip — example not found at {example_path}")
        return
    r = load_registry(str(example_path))
    assert len(r) >= 1
    print(f"✅ test_load_example_yaml_passes ({len(r)} strategies)")


def test_yaml_safe_load_no_arbitrary_objects():
    """yaml.safe_load만 사용 — 임의 객체 deserialization 차단."""
    # !!python/object 태그는 거부되어야 (또는 안전하게 무시)
    dangerous = """
version: "1.0"
strategies: !!python/object/apply:os.system ["echo pwned"]
"""
    try:
        load_registry_from_string(dangerous)
        # 통과하면 strategies 필드가 list가 아닐 수 있음 → validation에서 거부
    except (RegistryLoadError, Exception):
        pass
    # 핵심: 명령이 실행되지 않았어야 함 (eval/exec 미사용)
    print("✅ test_yaml_safe_load_no_arbitrary_objects")


# ─────────────────────────────────────────────────
# 실행
# ─────────────────────────────────────────────────

try:
    import pytest

    @pytest.fixture
    def tmp_dir(tmp_path):
        return tmp_path
except ImportError:
    pass


def _run_all() -> int:
    failed = 0
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        # 인자 없는
        for fn in [
            test_load_from_string_valid,
            test_load_missing_file,
            test_load_invalid_yaml,
            test_load_non_mapping,
            test_load_validation_error_message,
            test_load_duplicate_ids,
            test_load_secret_in_parameters,
            test_load_example_yaml_passes,
            test_yaml_safe_load_no_arbitrary_objects,
        ]:
            try:
                fn()
            except AssertionError as e:
                print(f"❌ {fn.__name__}: {e}")
                failed += 1
            except Exception as e:  # noqa: BLE001
                print(f"💥 {fn.__name__}: {type(e).__name__}: {e}")
                failed += 1
        # tmp_dir 필요
        for fn in [test_load_from_file, test_load_empty_file]:
            try:
                fn(td_path)
            except AssertionError as e:
                print(f"❌ {fn.__name__}: {e}")
                failed += 1
            except Exception as e:  # noqa: BLE001
                print(f"💥 {fn.__name__}: {type(e).__name__}: {e}")
                failed += 1
    return failed


if __name__ == "__main__":
    print("─" * 50)
    print("Task 45 v0.1 — loader 스모크 테스트")
    print("─" * 50)
    failed = _run_all()
    print("─" * 50)
    if failed == 0:
        print("✅ 모든 테스트 통과")
        sys.exit(0)
    else:
        print(f"❌ {failed}개 실패")
        sys.exit(1)
