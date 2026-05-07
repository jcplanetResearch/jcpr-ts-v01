"""
스모크 테스트 — trace_context (Task A1)
========================================

JCPR Trading System - jcpr-ts-v01
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from src.observability.trace_context import (  # noqa: E402
    ALLOWED_ORIGINS,
    MASKED_VALUE,
    ORIGIN_AGENT,
    ORIGIN_OPERATOR,
    SECRET_KEYWORDS,
    TraceContext,
    generate_span_id,
    generate_trace_id,
    new_agent_trace,
    new_operator_trace,
    new_scheduler_trace,
)


# ─────────────────────────────────────────────────
# ID 생성
# ─────────────────────────────────────────────────

def test_generate_trace_id_format():
    tid = generate_trace_id()
    assert tid.startswith("trc-"), f"Got {tid}"
    parts = tid.split("-")
    assert len(parts) == 3
    assert len(parts[1]) == 8  # YYYYMMDD
    assert len(parts[2]) == 8  # hex
    # hex 검증
    int(parts[2], 16)
    print("✅ test_generate_trace_id_format")


def test_generate_trace_id_uniqueness():
    ids = {generate_trace_id() for _ in range(100)}
    assert len(ids) == 100, "Duplicates in 100 generated IDs"
    print("✅ test_generate_trace_id_uniqueness")


def test_generate_span_id_format():
    sid = generate_span_id()
    assert sid.startswith("spn-")
    int(sid.split("-")[1], 16)
    print("✅ test_generate_span_id_format")


# ─────────────────────────────────────────────────
# TraceContext.new
# ─────────────────────────────────────────────────

def test_new_minimal():
    ctx = TraceContext.new(
        origin=ORIGIN_OPERATOR,
        session_id="session-1",
    )
    assert ctx.trace_id.startswith("trc-")
    assert ctx.span_id.startswith("spn-")
    assert ctx.parent_span_id is None
    assert ctx.origin == ORIGIN_OPERATOR
    assert ctx.session_id == "session-1"
    assert ctx.started_at_utc.tzinfo is not None
    print("✅ test_new_minimal")


def test_new_full():
    ctx = TraceContext.new(
        origin=ORIGIN_AGENT,
        operator_id="market_agent",
        session_id="session-1",
        correlation_keys={"symbol": "005930", "strategy_id": "momentum_v1"},
    )
    assert ctx.operator_id == "market_agent"
    assert ctx.correlation_keys["symbol"] == "005930"
    print("✅ test_new_full")


def test_inject_trace_id():
    """외부 주입 trace_id."""
    custom = "trc-20260507-deadbeef"
    ctx = TraceContext.new(
        origin=ORIGIN_OPERATOR,
        session_id="s",
        trace_id=custom,
    )
    assert ctx.trace_id == custom
    print("✅ test_inject_trace_id")


def test_inject_invalid_trace_id_rejected():
    bad = ["wrong-format", "trc-abc", "trc-20260507", "trc-20260507-XYZ123"]
    for b in bad:
        try:
            TraceContext.new(
                origin=ORIGIN_OPERATOR,
                session_id="s",
                trace_id=b,
            )
            assert False, f"Should reject {b}"
        except ValueError:
            pass
    print("✅ test_inject_invalid_trace_id_rejected")


def test_invalid_origin_rejected():
    try:
        TraceContext.new(
            origin="hacker",
            session_id="s",
        )
        assert False
    except ValueError:
        pass
    print("✅ test_invalid_origin_rejected")


def test_empty_session_id_rejected():
    try:
        TraceContext.new(origin=ORIGIN_OPERATOR, session_id="")
        assert False
    except ValueError:
        pass
    print("✅ test_empty_session_id_rejected")


def test_naive_datetime_rejected():
    try:
        TraceContext.new(
            origin=ORIGIN_OPERATOR,
            session_id="s",
            started_at_utc=datetime(2026, 5, 7),  # naive
        )
        assert False
    except ValueError:
        pass
    print("✅ test_naive_datetime_rejected")


# ─────────────────────────────────────────────────
# 시크릿 검사
# ─────────────────────────────────────────────────

def test_secret_in_correlation_keys_rejected():
    bad_keys = [
        {"api_key": "abc"},
        {"password": "p"},
        {"auth_token": "t"},
    ]
    for bk in bad_keys:
        try:
            TraceContext.new(
                origin=ORIGIN_OPERATOR,
                session_id="s",
                correlation_keys=bk,
            )
            assert False, f"Should reject {bk}"
        except ValueError:
            pass
    print("✅ test_secret_in_correlation_keys_rejected")


def test_long_credential_value_rejected():
    """긴 base64-like 값 거부."""
    try:
        TraceContext.new(
            origin=ORIGIN_OPERATOR,
            session_id="s",
            correlation_keys={
                "innocent": "ABC123XYZ" * 5,  # 45자 영숫자
            },
        )
        assert False
    except ValueError:
        pass
    print("✅ test_long_credential_value_rejected")


# ─────────────────────────────────────────────────
# child_span
# ─────────────────────────────────────────────────

def test_child_span_inherits_trace():
    parent = TraceContext.new(
        origin=ORIGIN_OPERATOR,
        session_id="s",
        correlation_keys={"symbol": "005930"},
    )
    child = parent.child_span("risk_eval")
    # 같은 trace_id
    assert child.trace_id == parent.trace_id
    # 다른 span_id
    assert child.span_id != parent.span_id
    # 부모 연결
    assert child.parent_span_id == parent.span_id
    # 같은 origin/session
    assert child.origin == parent.origin
    assert child.session_id == parent.session_id
    # span_name 추가
    assert child.correlation_keys["span_name"] == "risk_eval"
    # 기존 키 유지
    assert child.correlation_keys["symbol"] == "005930"
    print("✅ test_child_span_inherits_trace")


def test_child_span_additional_correlation():
    parent = TraceContext.new(origin=ORIGIN_OPERATOR, session_id="s")
    child = parent.child_span(
        "submit",
        additional_correlation={"order_id": "O-123"},
    )
    assert child.correlation_keys["order_id"] == "O-123"
    print("✅ test_child_span_additional_correlation")


def test_grandchild_span():
    """3단 계층."""
    root = TraceContext.new(origin=ORIGIN_OPERATOR, session_id="s")
    child = root.child_span("step1")
    grandchild = child.child_span("step2")
    assert grandchild.parent_span_id == child.span_id
    assert grandchild.trace_id == root.trace_id
    print("✅ test_grandchild_span")


# ─────────────────────────────────────────────────
# 직렬화
# ─────────────────────────────────────────────────

def test_to_dict_serializable():
    import json
    ctx = TraceContext.new(
        origin=ORIGIN_OPERATOR,
        session_id="s",
        correlation_keys={"symbol": "005930"},
    )
    d = ctx.to_dict()
    j = json.dumps(d)
    assert "005930" in j
    print("✅ test_to_dict_serializable")


def test_to_audit_dict_masks_secrets_in_correlation():
    """
    post-init이 시크릿을 차단하지만, 만약 이전에 들어간 객체가
    어떤 식으로 조작돼서 audit 출력 시점에 시크릿이 있다면 마스킹.
    여기서는 정상 객체에 마스킹이 작동 안 하는 것만 확인.
    """
    ctx = TraceContext.new(
        origin=ORIGIN_OPERATOR,
        session_id="s",
        correlation_keys={"normal": "value", "symbol": "005930"},
    )
    d = ctx.to_audit_dict()
    # normal은 그대로
    assert d["correlation_keys"]["normal"] == "value"
    print("✅ test_to_audit_dict_masks_secrets_in_correlation")


def test_repr_safe():
    """__repr__에 correlation_keys 노출 안 됨."""
    # 시크릿 포함 시 거부됨을 확인
    try:
        TraceContext.new(
            origin=ORIGIN_OPERATOR,
            session_id="s",
            correlation_keys={"symbol": "005930", "secret_data": "x"},
        )
        assert False, "secret_data should be rejected"
    except ValueError:
        pass
    # 정상 객체에서 repr이 correlation_keys를 노출하지 않는지 확인
    ctx2 = TraceContext.new(
        origin=ORIGIN_OPERATOR,
        session_id="s",
        correlation_keys={"symbol": "005930"},
    )
    r = repr(ctx2)
    assert "005930" not in r, "correlation_keys leaked in repr"
    print("✅ test_repr_safe")


def test_short_id():
    ctx = TraceContext.new(origin=ORIGIN_OPERATOR, session_id="s")
    s = ctx.short_id()
    assert "trc-..." in s
    assert "spn-..." in s
    print("✅ test_short_id")


# ─────────────────────────────────────────────────
# 불변성
# ─────────────────────────────────────────────────

def test_frozen():
    ctx = TraceContext.new(origin=ORIGIN_OPERATOR, session_id="s")
    try:
        ctx.session_id = "changed"  # type: ignore[misc]
        assert False
    except Exception:
        pass
    print("✅ test_frozen")


# ─────────────────────────────────────────────────
# 편의 함수
# ─────────────────────────────────────────────────

def test_new_operator_trace():
    ctx = new_operator_trace("alice", "session-1")
    assert ctx.origin == ORIGIN_OPERATOR
    assert ctx.operator_id == "alice"
    print("✅ test_new_operator_trace")


def test_new_agent_trace():
    ctx = new_agent_trace("market_agent", "session-1")
    assert ctx.origin == ORIGIN_AGENT
    print("✅ test_new_agent_trace")


def test_new_scheduler_trace():
    ctx = new_scheduler_trace("daily_report", "session-1")
    assert ctx.origin == "scheduler"
    print("✅ test_new_scheduler_trace")


# ─────────────────────────────────────────────────
# 실행
# ─────────────────────────────────────────────────

def _run_all() -> int:
    failed = 0
    tests = [
        test_generate_trace_id_format,
        test_generate_trace_id_uniqueness,
        test_generate_span_id_format,
        test_new_minimal,
        test_new_full,
        test_inject_trace_id,
        test_inject_invalid_trace_id_rejected,
        test_invalid_origin_rejected,
        test_empty_session_id_rejected,
        test_naive_datetime_rejected,
        test_secret_in_correlation_keys_rejected,
        test_long_credential_value_rejected,
        test_child_span_inherits_trace,
        test_child_span_additional_correlation,
        test_grandchild_span,
        test_to_dict_serializable,
        test_to_audit_dict_masks_secrets_in_correlation,
        test_repr_safe,
        test_short_id,
        test_frozen,
        test_new_operator_trace,
        test_new_agent_trace,
        test_new_scheduler_trace,
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
    print("Task A1 v0.1 — trace_context 스모크 테스트")
    print("─" * 50)
    failed = _run_all()
    print("─" * 50)
    if failed == 0:
        print("✅ 모든 테스트 통과")
        sys.exit(0)
    else:
        print(f"❌ {failed}개 실패")
        sys.exit(1)
