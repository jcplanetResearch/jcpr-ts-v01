"""
스모크 테스트 — _mcp_client (Task 37)
======================================

In-process MCP client 검증.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from src.agents._mcp_client import (  # noqa: E402
    MCPCallResult,
    MCPReadOnlyClient,
)
from src.mcp_servers import ReadOnlyServerConfig  # noqa: E402
from src.observability import (  # noqa: E402
    configure_default_writer,
    reset_default_writer,
)


def _config(tmp_dir: Path) -> ReadOnlyServerConfig:
    audit_dir = tmp_dir / "audit"
    audit_dir.mkdir(exist_ok=True)
    return ReadOnlyServerConfig(
        audit_dir=str(audit_dir),
        session_id="test-session",
    )


def _setup(tmp_dir: Path) -> MCPReadOnlyClient:
    reset_default_writer()
    configure_default_writer(str(tmp_dir / "audit"))
    return MCPReadOnlyClient(
        config=_config(tmp_dir),
        agent_name="test_agent",
    )


# ─────────────────────────────────────────────────
# 도구별 호출
# ─────────────────────────────────────────────────

def test_get_market_status(tmp_dir):
    client = _setup(tmp_dir)
    result = client.get_market_status()
    assert isinstance(result, MCPCallResult)
    assert result.tool_name == "get_market_status"
    assert result.success or result.error_code  # 결과가 있어야 함
    assert result.trace_id  # trace 생성됨
    assert result.elapsed_ms >= 0
    print("✅ test_get_market_status")


def test_get_pnl_snapshot_basic(tmp_dir):
    client = _setup(tmp_dir)
    result = client.get_pnl_snapshot(
        starting_capital_krw="10000000",
        cash_krw="500000",
    )
    assert result.tool_name == "get_pnl_snapshot"
    # DB 없으면 fail이지만 호출 자체는 작동
    assert result.trace_id
    print("✅ test_get_pnl_snapshot_basic")


def test_get_strategy_registry(tmp_dir):
    client = _setup(tmp_dir)
    result = client.get_strategy_registry()
    assert result.tool_name == "get_strategy_registry"
    assert result.trace_id
    print("✅ test_get_strategy_registry")


def test_get_recent_fills(tmp_dir):
    client = _setup(tmp_dir)
    result = client.get_recent_fills(limit=10)
    assert result.tool_name == "get_recent_fills"
    print("✅ test_get_recent_fills")


def test_get_rejection_summary(tmp_dir):
    client = _setup(tmp_dir)
    result = client.get_rejection_summary()
    assert result.tool_name == "get_rejection_summary"
    print("✅ test_get_rejection_summary")


def test_get_portfolio_risk(tmp_dir):
    client = _setup(tmp_dir)
    result = client.get_portfolio_risk(
        sector_map={"005930": "tech"},
        cash_krw="500000",
    )
    assert result.tool_name == "get_portfolio_risk"
    print("✅ test_get_portfolio_risk")


def test_get_positions(tmp_dir):
    client = _setup(tmp_dir)
    result = client.get_positions()
    assert result.tool_name == "get_positions"
    print("✅ test_get_positions")


def test_get_trace(tmp_dir):
    client = _setup(tmp_dir)
    # 존재하지 않는 trace_id — error 응답이 와야 함 (예외 안 던짐)
    result = client.get_trace(trace_id="trc-19990101-deadbeef")
    assert result.tool_name == "get_trace"
    # 존재 안 함 → success=False 또는 ok=False
    print("✅ test_get_trace")


# ─────────────────────────────────────────────────
# 일반화 호출
# ─────────────────────────────────────────────────

def test_call_known_tool(tmp_dir):
    client = _setup(tmp_dir)
    result = client.call("get_market_status")
    assert result.tool_name == "get_market_status"
    assert result.trace_id
    print("✅ test_call_known_tool")


def test_call_unknown_tool(tmp_dir):
    client = _setup(tmp_dir)
    result = client.call("nonexistent_tool")
    assert not result.success
    assert result.error_code == "UNKNOWN_TOOL"
    print("✅ test_call_unknown_tool")


def test_call_write_tool_blocked(tmp_dir):
    """write 도구 (Task 35) 호출 시도 차단."""
    client = _setup(tmp_dir)
    # write 도구 이름 — 구현 안 됨
    for write_tool in (
        "request_submit_order",
        "request_cancel_order",
        "execute_approved_action",
        "approve_action",
    ):
        result = client.call(write_tool)
        assert not result.success
        assert result.error_code == "UNKNOWN_TOOL", \
            f"write tool {write_tool} should not be callable"
    print("✅ test_call_write_tool_blocked")


# ─────────────────────────────────────────────────
# Audit 기록
# ─────────────────────────────────────────────────

def test_audit_recorded(tmp_dir):
    """호출 시 audit 이벤트 기록."""
    from src.observability import AuditIndexer

    client = _setup(tmp_dir)
    result = client.get_market_status()

    indexer = AuditIndexer(audit_dir=str(tmp_dir / "audit"))
    events = indexer.find_by_trace(result.trace_id)
    types = [e.event_type for e in events]
    assert "mcp_tool_call" in types
    assert "mcp_tool_result" in types
    print("✅ test_audit_recorded")


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
        test_get_market_status, test_get_pnl_snapshot_basic,
        test_get_strategy_registry, test_get_recent_fills,
        test_get_rejection_summary, test_get_portfolio_risk,
        test_get_positions, test_get_trace,
        test_call_known_tool, test_call_unknown_tool,
        test_call_write_tool_blocked, test_audit_recorded,
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
    print("Task 37 v0.1 — _mcp_client 테스트")
    print("─" * 50)
    failed = _run_all()
    print("─" * 50)
    if failed == 0:
        print("✅ 모든 테스트 통과")
        sys.exit(0)
    else:
        print(f"❌ {failed}개 실패")
        sys.exit(1)
