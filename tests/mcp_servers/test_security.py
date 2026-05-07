"""
스모크 테스트 — _security
==========================

JCPR Trading System - jcpr-ts-v01
Task 34 v0.1
"""

from __future__ import annotations

import sys
import time
from decimal import Decimal
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from src.mcp_servers._security import (  # noqa: E402
    MAX_RESULT_BYTES,
    RateLimiter,
    check_result_size,
    mask_output,
    validate_iso_datetime,
    validate_limit,
    validate_sector_map,
    validate_symbol,
    validate_trace_id,
)


# ─────────────────────────────────────────────────
# 입력 검증
# ─────────────────────────────────────────────────

def test_validate_symbol_valid():
    assert validate_symbol("005930") == "005930"
    assert validate_symbol("AAPL") == "AAPL"
    assert validate_symbol("kodex.200") == "KODEX.200"
    assert validate_symbol("  005930  ") == "005930"
    assert validate_symbol(None) is None
    print("✅ test_validate_symbol_valid")


def test_validate_symbol_invalid():
    bad = ["with space", "한글", "with/slash", "x" * 20, ""]
    for b in bad:
        try:
            validate_symbol(b)
            assert False, f"Should reject {b!r}"
        except ValueError:
            pass
    print("✅ test_validate_symbol_invalid")


def test_validate_trace_id_valid():
    assert validate_trace_id("trc-20260507-a1b2c3d4") == "trc-20260507-a1b2c3d4"
    assert validate_trace_id(None) is None
    print("✅ test_validate_trace_id_valid")


def test_validate_trace_id_invalid():
    bad = ["wrong", "trc-abc", "trc-20260507", "trc-20260507-XYZ"]
    for b in bad:
        try:
            validate_trace_id(b)
            assert False
        except ValueError:
            pass
    print("✅ test_validate_trace_id_invalid")


def test_validate_iso_datetime():
    assert validate_iso_datetime("2026-05-07T12:00:00") == "2026-05-07T12:00:00"
    assert validate_iso_datetime("2026-05-07T12:00:00Z") == "2026-05-07T12:00:00Z"
    assert validate_iso_datetime("2026-05-07T12:00:00+09:00") == "2026-05-07T12:00:00+09:00"
    assert validate_iso_datetime(None) is None
    try:
        validate_iso_datetime("not-a-date")
        assert False
    except ValueError:
        pass
    print("✅ test_validate_iso_datetime")


def test_validate_limit():
    assert validate_limit(None, default=50, max_value=500) == 50
    assert validate_limit(100, default=50, max_value=500) == 100
    try:
        validate_limit(0, default=50, max_value=500)
        assert False
    except ValueError:
        pass
    try:
        validate_limit(1000, default=50, max_value=500)
        assert False
    except ValueError:
        pass
    print("✅ test_validate_limit")


def test_validate_sector_map():
    assert validate_sector_map(None) == {}
    assert validate_sector_map({}) == {}
    out = validate_sector_map({"005930": "tech", "069500": "etf"})
    assert out == {"005930": "tech", "069500": "etf"}
    # 잘못된 sector
    try:
        validate_sector_map({"005930": "with space"})
        assert False
    except ValueError:
        pass
    # 잘못된 symbol
    try:
        validate_sector_map({"with space": "tech"})
        assert False
    except ValueError:
        pass
    print("✅ test_validate_sector_map")


def test_sector_map_too_large():
    big = {f"S{i:05d}": "tech" for i in range(1100)}
    try:
        validate_sector_map(big)
        assert False
    except ValueError:
        pass
    print("✅ test_sector_map_too_large")


# ─────────────────────────────────────────────────
# 마스킹
# ─────────────────────────────────────────────────

def test_mask_output_secrets():
    """시크릿 자동 마스킹."""
    inp = {
        "normal": "value",
        "api_key": "ABC123",
        "nested": {"password": "secret"},
    }
    out = mask_output(inp)
    assert out["normal"] == "value"
    assert "MASKED" in str(out["api_key"])
    assert "MASKED" in str(out["nested"]["password"])
    print("✅ test_mask_output_secrets")


def test_mask_output_pii():
    """PII 마스킹."""
    inp = {
        "symbol": "005930",
        "operator_id_full": "alice@company.com",
        "account_number": "123-456-789",
        "phone": "010-1234-5678",
    }
    out = mask_output(inp)
    assert out["symbol"] == "005930"  # 정상
    assert "PII_MASKED" in str(out["operator_id_full"])
    assert "PII_MASKED" in str(out["account_number"])
    assert "PII_MASKED" in str(out["phone"])
    print("✅ test_mask_output_pii")


def test_mask_output_decimal_to_str():
    """Decimal → str 변환."""
    inp = {"price": Decimal("70000.50"), "qty": Decimal("100")}
    out = mask_output(inp)
    assert out["price"] == "70000.50"
    assert out["qty"] == "100"
    print("✅ test_mask_output_decimal_to_str")


def test_mask_output_nested():
    """중첩 dict/list."""
    inp = {
        "items": [
            {"api_key": "x", "ok": "v1"},
            {"normal": "v2"},
        ],
    }
    out = mask_output(inp)
    assert "MASKED" in str(out["items"][0]["api_key"])
    assert out["items"][0]["ok"] == "v1"
    assert out["items"][1]["normal"] == "v2"
    print("✅ test_mask_output_nested")


# ─────────────────────────────────────────────────
# 결과 크기
# ─────────────────────────────────────────────────

def test_check_result_size_ok():
    small = "x" * 100
    ok, msg = check_result_size(small)
    assert ok is True
    assert msg is None
    print("✅ test_check_result_size_ok")


def test_check_result_size_too_large():
    big = "x" * (MAX_RESULT_BYTES + 100)
    ok, msg = check_result_size(big)
    assert ok is False
    assert "크기" in msg or "size" in msg.lower()
    print("✅ test_check_result_size_too_large")


# ─────────────────────────────────────────────────
# Rate Limiter
# ─────────────────────────────────────────────────

def test_rate_limiter_under_limit():
    rl = RateLimiter(max_per_minute=10)
    for _ in range(5):
        ok, msg = rl.check()
        assert ok is True
        assert msg is None
    assert rl.current_count() == 5
    print("✅ test_rate_limiter_under_limit")


def test_rate_limiter_over_limit():
    rl = RateLimiter(max_per_minute=3)
    for _ in range(3):
        ok, _ = rl.check()
        assert ok is True
    # 4번째는 거부
    ok, msg = rl.check()
    assert ok is False
    assert "Rate limit" in msg or "rate" in msg.lower()
    print("✅ test_rate_limiter_over_limit")


def test_rate_limiter_invalid_max():
    try:
        RateLimiter(max_per_minute=0)
        assert False
    except ValueError:
        pass
    print("✅ test_rate_limiter_invalid_max")


# ─────────────────────────────────────────────────
# 실행
# ─────────────────────────────────────────────────

def _run_all() -> int:
    failed = 0
    tests = [
        test_validate_symbol_valid,
        test_validate_symbol_invalid,
        test_validate_trace_id_valid,
        test_validate_trace_id_invalid,
        test_validate_iso_datetime,
        test_validate_limit,
        test_validate_sector_map,
        test_sector_map_too_large,
        test_mask_output_secrets,
        test_mask_output_pii,
        test_mask_output_decimal_to_str,
        test_mask_output_nested,
        test_check_result_size_ok,
        test_check_result_size_too_large,
        test_rate_limiter_under_limit,
        test_rate_limiter_over_limit,
        test_rate_limiter_invalid_max,
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
    print("Task 34 v0.1 — _security 스모크 테스트")
    print("─" * 50)
    failed = _run_all()
    print("─" * 50)
    if failed == 0:
        print("✅ 모든 테스트 통과")
        sys.exit(0)
    else:
        print(f"❌ {failed}개 실패")
        sys.exit(1)
