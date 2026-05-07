"""
스모크 테스트 — _write_handlers + restricted_server (통합)
=============================================================

JCPR Trading System - jcpr-ts-v01
Task 35 v0.1
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from src.mcp_servers._approval_store import (  # noqa: E402
    ApprovalStore,
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_EXECUTED,
)
from src.mcp_servers._config import RestrictedServerConfig  # noqa: E402
from src.mcp_servers import _write_handlers as wh  # noqa: E402


# ─────────────────────────────────────────────────
# 헬퍼
# ─────────────────────────────────────────────────

def _config(tmp_dir: Path, *, allow_live: bool = False,
            allow_self_approval: bool = False) -> RestrictedServerConfig:
    return RestrictedServerConfig(
        audit_dir=str(tmp_dir / "audit"),
        approval_db=str(tmp_dir / "ap.db"),
        session_id="test-session",
        operator_id="operator-test",
        allow_live=allow_live,
        allow_self_approval=allow_self_approval,
        approval_ttl_seconds=300,
        execute_ttl_seconds=60,
    )


def _store(tmp_dir: Path, *, allow_self_approval: bool = False) -> ApprovalStore:
    (tmp_dir / "audit").mkdir(exist_ok=True)
    return ApprovalStore(
        db_path=str(tmp_dir / "ap.db"),
        allow_self_approval=allow_self_approval,
    )


def _trace_id() -> str:
    from src.observability import generate_trace_id
    return generate_trace_id()


# ─────────────────────────────────────────────────
# request_submit_order
# ─────────────────────────────────────────────────

def test_request_submit_order_paper(tmp_dir):
    cfg = _config(tmp_dir)
    store = _store(tmp_dir)
    res = wh.request_submit_order(
        cfg, store,
        symbol="005930", side="buy", qty=10,
        order_type="market", mode="paper",
        requested_by="agent_alice",
        trace_id=_trace_id(),
    )
    assert res["ok"] is True
    assert res["status"] == STATUS_PENDING
    assert res["paper_mode"] is True
    assert res["payload"]["symbol"] == "005930"
    assert "approval_id" in res
    print("✅ test_request_submit_order_paper")


def test_request_submit_order_live_blocked_default(tmp_dir):
    """allow_live=False (default) — live 요청 거부."""
    cfg = _config(tmp_dir, allow_live=False)
    store = _store(tmp_dir)
    res = wh.request_submit_order(
        cfg, store,
        symbol="005930", side="buy", qty=10,
        order_type="market", mode="live",  # ← live 요청
        requested_by="agent",
        trace_id=_trace_id(),
    )
    assert res["ok"] is False
    assert res["error_code"] == "VALIDATION_ERROR"
    assert "live" in res["error_message"].lower()
    print("✅ test_request_submit_order_live_blocked_default")


def test_request_submit_order_live_allowed_when_enabled(tmp_dir):
    cfg = _config(tmp_dir, allow_live=True)
    store = _store(tmp_dir)
    res = wh.request_submit_order(
        cfg, store,
        symbol="005930", side="buy", qty=10,
        order_type="market", mode="live",
        requested_by="agent",
        trace_id=_trace_id(),
    )
    assert res["ok"] is True
    assert res["paper_mode"] is False
    print("✅ test_request_submit_order_live_allowed_when_enabled")


def test_request_submit_order_invalid_qty(tmp_dir):
    cfg = _config(tmp_dir)
    store = _store(tmp_dir)
    res = wh.request_submit_order(
        cfg, store,
        symbol="005930", side="buy", qty=0,  # invalid
        requested_by="agent", trace_id=_trace_id(),
    )
    assert res["ok"] is False
    assert res["error_code"] == "VALIDATION_ERROR"
    print("✅ test_request_submit_order_invalid_qty")


def test_request_submit_order_limit_requires_price(tmp_dir):
    cfg = _config(tmp_dir)
    store = _store(tmp_dir)
    res = wh.request_submit_order(
        cfg, store,
        symbol="005930", side="buy", qty=10,
        order_type="limit",  # price_krw 누락
        requested_by="agent", trace_id=_trace_id(),
    )
    assert res["ok"] is False
    assert "limit" in res["error_message"].lower() or "price" in res["error_message"].lower()
    print("✅ test_request_submit_order_limit_requires_price")


def test_request_submit_order_limit_with_price(tmp_dir):
    cfg = _config(tmp_dir)
    store = _store(tmp_dir)
    res = wh.request_submit_order(
        cfg, store,
        symbol="005930", side="sell", qty=5,
        order_type="limit", price_krw="70000",
        requested_by="agent", trace_id=_trace_id(),
    )
    assert res["ok"] is True
    assert res["payload"]["price_krw"] == "70000"
    print("✅ test_request_submit_order_limit_with_price")


# ─────────────────────────────────────────────────
# request_cancel / set_capacity / kill_switch
# ─────────────────────────────────────────────────

def test_request_cancel_order(tmp_dir):
    cfg = _config(tmp_dir)
    store = _store(tmp_dir)
    res = wh.request_cancel_order(
        cfg, store,
        order_id="ORDER123",
        reason="strategy change",
        requested_by="agent",
        trace_id=_trace_id(),
    )
    assert res["ok"] is True
    assert res["action_type"] == "cancel_order"
    print("✅ test_request_cancel_order")


def test_request_set_capacity(tmp_dir):
    cfg = _config(tmp_dir)
    store = _store(tmp_dir)
    res = wh.request_set_capacity(
        cfg, store,
        capacity_krw="100000000",
        target="total",
        reason="rebalance",
        requested_by="agent",
        trace_id=_trace_id(),
    )
    assert res["ok"] is True
    assert res["payload"]["capacity_krw"] == "100000000"
    print("✅ test_request_set_capacity")


def test_request_set_capacity_per_strategy_requires_id(tmp_dir):
    cfg = _config(tmp_dir)
    store = _store(tmp_dir)
    res = wh.request_set_capacity(
        cfg, store,
        capacity_krw="50000000",
        target="per_strategy",
        # strategy_id 누락
        requested_by="agent", trace_id=_trace_id(),
    )
    assert res["ok"] is False
    print("✅ test_request_set_capacity_per_strategy_requires_id")


def test_request_kill_switch(tmp_dir):
    cfg = _config(tmp_dir)
    store = _store(tmp_dir)
    res = wh.request_kill_switch(
        cfg, store,
        activate=True,
        reason="emergency stop",
        requested_by="agent",
        trace_id=_trace_id(),
    )
    assert res["ok"] is True
    assert res["payload"]["activate"] is True
    print("✅ test_request_kill_switch")


def test_request_kill_switch_activate_requires_reason(tmp_dir):
    cfg = _config(tmp_dir)
    store = _store(tmp_dir)
    res = wh.request_kill_switch(
        cfg, store,
        activate=True, reason="",  # 빈 reason
        requested_by="agent", trace_id=_trace_id(),
    )
    assert res["ok"] is False
    print("✅ test_request_kill_switch_activate_requires_reason")


# ─────────────────────────────────────────────────
# 관리 도구
# ─────────────────────────────────────────────────

def test_list_pending_approvals(tmp_dir):
    cfg = _config(tmp_dir)
    store = _store(tmp_dir)
    for i in range(3):
        wh.request_submit_order(
            cfg, store,
            symbol="005930", side="buy", qty=10 + i,
            requested_by=f"agent_{i}", trace_id=_trace_id(),
        )
    res = wh.list_pending_approvals(cfg, store)
    assert res["ok"] is True
    assert res["count"] == 3
    print("✅ test_list_pending_approvals")


def test_get_approval_status(tmp_dir):
    cfg = _config(tmp_dir)
    store = _store(tmp_dir)
    r1 = wh.request_submit_order(
        cfg, store,
        symbol="005930", side="buy", qty=10,
        requested_by="agent", trace_id=_trace_id(),
    )
    aid = r1["approval_id"]
    res = wh.get_approval_status(cfg, store, approval_id=aid)
    assert res["ok"] is True
    assert res["approval_id"] == aid
    print("✅ test_get_approval_status")


def test_get_approval_status_not_found(tmp_dir):
    cfg = _config(tmp_dir)
    store = _store(tmp_dir)
    res = wh.get_approval_status(
        cfg, store, approval_id="apv-20991231-deadbeef",
    )
    assert res["ok"] is False
    assert res["error_code"] == "NOT_FOUND"
    print("✅ test_get_approval_status_not_found")


def test_get_approval_status_invalid_id(tmp_dir):
    cfg = _config(tmp_dir)
    store = _store(tmp_dir)
    res = wh.get_approval_status(cfg, store, approval_id="bad-format")
    assert res["ok"] is False
    assert res["error_code"] == "VALIDATION_ERROR"
    print("✅ test_get_approval_status_invalid_id")


def test_cancel_request_by_owner(tmp_dir):
    cfg = _config(tmp_dir)
    store = _store(tmp_dir)
    r1 = wh.request_submit_order(
        cfg, store,
        symbol="005930", side="buy", qty=10,
        requested_by="agent_alice", trace_id=_trace_id(),
    )
    res = wh.cancel_request(
        cfg, store,
        approval_id=r1["approval_id"],
        cancelled_by="agent_alice",
    )
    assert res["ok"] is True
    assert res["status"] == "cancelled"
    print("✅ test_cancel_request_by_owner")


def test_cancel_request_by_other_blocked(tmp_dir):
    cfg = _config(tmp_dir)
    store = _store(tmp_dir)
    r1 = wh.request_submit_order(
        cfg, store,
        symbol="005930", side="buy", qty=10,
        requested_by="agent_alice", trace_id=_trace_id(),
    )
    res = wh.cancel_request(
        cfg, store,
        approval_id=r1["approval_id"],
        cancelled_by="agent_bob",  # 다른 사람
    )
    assert res["ok"] is False
    print("✅ test_cancel_request_by_other_blocked")


# ─────────────────────────────────────────────────
# 실행 + 승인 플로우
# ─────────────────────────────────────────────────

def test_full_flow_request_approve_execute(tmp_dir):
    """전체 플로우: 요청 → 승인(다른 사용자) → 실행."""
    cfg = _config(tmp_dir)
    store = _store(tmp_dir)
    # 1. agent가 요청
    r1 = wh.request_submit_order(
        cfg, store,
        symbol="005930", side="buy", qty=10,
        requested_by="agent_alice", trace_id=_trace_id(),
    )
    aid = r1["approval_id"]
    assert r1["status"] == STATUS_PENDING

    # 2. 운영자가 승인
    r2 = wh.approve_action(
        cfg, store,
        approval_id=aid,
        decided_by="operator_bob",
        reason="verified",
    )
    assert r2["ok"] is True
    assert r2["status"] == STATUS_APPROVED

    # 3. agent가 실행
    r3 = wh.execute_approved_action(
        cfg, store,
        approval_id=aid,
        executed_by="agent_alice",
    )
    assert r3["ok"] is True
    assert r3["status"] == STATUS_EXECUTED
    # stub 결과
    assert r3["execution_result"]["stub"] is True
    print("✅ test_full_flow_request_approve_execute")


def test_self_approval_blocked_in_handler(tmp_dir):
    cfg = _config(tmp_dir)
    store = _store(tmp_dir)
    r1 = wh.request_submit_order(
        cfg, store,
        symbol="005930", side="buy", qty=10,
        requested_by="alice", trace_id=_trace_id(),
    )
    res = wh.approve_action(
        cfg, store,
        approval_id=r1["approval_id"],
        decided_by="alice",  # self
    )
    assert res["ok"] is False
    assert res["error_code"] == "SELF_APPROVAL_BLOCKED"
    print("✅ test_self_approval_blocked_in_handler")


def test_execute_without_approve_blocked(tmp_dir):
    cfg = _config(tmp_dir)
    store = _store(tmp_dir)
    r1 = wh.request_submit_order(
        cfg, store,
        symbol="005930", side="buy", qty=10,
        requested_by="agent", trace_id=_trace_id(),
    )
    res = wh.execute_approved_action(
        cfg, store,
        approval_id=r1["approval_id"],
        executed_by="agent",
    )
    assert res["ok"] is False
    assert res["error_code"] == "STATE_ERROR"
    print("✅ test_execute_without_approve_blocked")


def test_double_execute_blocked(tmp_dir):
    cfg = _config(tmp_dir)
    store = _store(tmp_dir)
    r1 = wh.request_submit_order(
        cfg, store,
        symbol="005930", side="buy", qty=10,
        requested_by="agent", trace_id=_trace_id(),
    )
    wh.approve_action(cfg, store, approval_id=r1["approval_id"],
                      decided_by="op")
    wh.execute_approved_action(
        cfg, store, approval_id=r1["approval_id"],
        executed_by="agent",
    )
    res = wh.execute_approved_action(
        cfg, store, approval_id=r1["approval_id"],
        executed_by="agent",
    )
    assert res["ok"] is False
    print("✅ test_double_execute_blocked")


# ─────────────────────────────────────────────────
# 서버 빌드
# ─────────────────────────────────────────────────

def test_build_restricted_server(tmp_dir):
    """8개 도구 등록 확인."""
    from src.mcp_servers import build_restricted_server
    from src.observability import reset_default_writer
    reset_default_writer()
    cfg = _config(tmp_dir)
    server, store = build_restricted_server(cfg)
    assert server is not None
    assert server.name == "jcpr-restricted"

    import asyncio
    tools = asyncio.run(server.list_tools())
    names = sorted(t.name for t in tools)
    expected = sorted([
        "request_submit_order",
        "request_cancel_order",
        "request_set_capacity",
        "request_kill_switch",
        "list_pending_approvals",
        "get_approval_status",
        "cancel_request",
        "execute_approved_action",
    ])
    assert names == expected, f"Got {names}"
    reset_default_writer()
    print(f"✅ test_build_restricted_server (8 tools)")


def test_server_audit_integration(tmp_dir):
    """tool 호출 시 audit log 기록 확인."""
    from src.mcp_servers import build_restricted_server
    from src.observability import (
        AuditIndexer, configure_default_writer, reset_default_writer,
    )
    import asyncio
    import json

    reset_default_writer()
    audit_dir = tmp_dir / "audit"
    audit_dir.mkdir(exist_ok=True)
    configure_default_writer(str(audit_dir))

    cfg = _config(tmp_dir)
    server, store = build_restricted_server(cfg)

    # 도구 호출
    async def call():
        result = await server.call_tool(
            "request_submit_order",
            arguments={
                "symbol": "005930", "side": "buy", "qty": 10,
                "order_type": "market", "mode": "paper",
                "requested_by": "agent_test",
            },
        )
        # MCP 1.27.0: list of TextContent blocks (or tuple in some versions)
        if isinstance(result, tuple):
            _, structured = result
            return structured
        if isinstance(result, list) and result:
            # Parse JSON from text content
            text = getattr(result[0], "text", None)
            if text:
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return None
        return None

    structured = asyncio.run(call())
    assert structured is not None, "tool returned no structured result"
    assert structured.get("ok") is True, f"call failed: {structured}"
    assert structured.get("status") == STATUS_PENDING
    trace_id = structured.get("_trace_id")
    assert trace_id, "no trace_id in result"

    # audit 확인
    indexer = AuditIndexer(audit_dir=str(audit_dir))
    events = indexer.find_by_trace(trace_id)
    types = [e.event_type for e in events]
    assert "mcp_tool_call" in types
    assert "mcp_tool_result" in types
    assert "approval_request" in types
    reset_default_writer()
    print("✅ test_server_audit_integration")


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
    tests = [
        test_request_submit_order_paper,
        test_request_submit_order_live_blocked_default,
        test_request_submit_order_live_allowed_when_enabled,
        test_request_submit_order_invalid_qty,
        test_request_submit_order_limit_requires_price,
        test_request_submit_order_limit_with_price,
        test_request_cancel_order,
        test_request_set_capacity,
        test_request_set_capacity_per_strategy_requires_id,
        test_request_kill_switch,
        test_request_kill_switch_activate_requires_reason,
        test_list_pending_approvals,
        test_get_approval_status,
        test_get_approval_status_not_found,
        test_get_approval_status_invalid_id,
        test_cancel_request_by_owner,
        test_cancel_request_by_other_blocked,
        test_full_flow_request_approve_execute,
        test_self_approval_blocked_in_handler,
        test_execute_without_approve_blocked,
        test_double_execute_blocked,
        test_build_restricted_server,
        test_server_audit_integration,
    ]
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        for fn in tests:
            sub = td_path / fn.__name__
            sub.mkdir()
            try:
                fn(sub)
            except AssertionError as e:
                print(f"❌ {fn.__name__}: {e}")
                failed += 1
            except Exception as e:  # noqa: BLE001
                print(f"💥 {fn.__name__}: {type(e).__name__}: {e}")
                failed += 1
    return failed


if __name__ == "__main__":
    print("─" * 50)
    print("Task 35 v0.1 — write_handlers + restricted_server 테스트")
    print("─" * 50)
    failed = _run_all()
    print("─" * 50)
    if failed == 0:
        print("✅ 모든 테스트 통과")
        sys.exit(0)
    else:
        print(f"❌ {failed}개 실패")
        sys.exit(1)
