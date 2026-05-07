"""
스모크 테스트 — market_agent (Task 37)
=======================================

Market Analyst Agent 통합 테스트.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from src.agents import (  # noqa: E402
    MarketAnalystAgent,
    MockLLMClient,
    MCPReadOnlyClient,
)
from src.mcp_servers import ReadOnlyServerConfig  # noqa: E402
from src.observability import (  # noqa: E402
    AuditIndexer,
    configure_default_writer,
    reset_default_writer,
)


def _setup_agent(tmp_dir: Path, *, llm_fail_mode: str = "none") -> MarketAnalystAgent:
    """공통 setup."""
    audit_dir = tmp_dir / "audit"
    audit_dir.mkdir(exist_ok=True)
    reset_default_writer()
    configure_default_writer(str(audit_dir))

    config = ReadOnlyServerConfig(
        audit_dir=str(audit_dir),
        session_id="test-session",
    )
    mcp = MCPReadOnlyClient(config=config, agent_name="market_analyst")
    llm = MockLLMClient(schema_based=True, fail_mode=llm_fail_mode)
    return MarketAnalystAgent(
        llm_client=llm,
        operator_id="test-operator",
        session_id="test-session",
        mcp_client=mcp,
    )


# ─────────────────────────────────────────────────
# 기본 실행
# ─────────────────────────────────────────────────

def test_summarize_market_basic(tmp_dir):
    agent = _setup_agent(tmp_dir)
    result = agent.summarize_market(
        starting_capital_krw="10000000",
        cash_krw="500000",
    )
    assert result.agent_name == "market_analyst"
    assert result.trace_id.startswith("trc-")
    assert result.tool_calls_count >= 3  # 최소 3개 (status, positions, pnl)
    print("✅ test_summarize_market_basic")


def test_summarize_with_query(tmp_dir):
    agent = _setup_agent(tmp_dir)
    result = agent.summarize_market(
        starting_capital_krw="10000000",
        cash_krw="500000",
        operator_query="오늘 어떻게 되어가나요?",
    )
    # operator_query는 LLM에 전달되지만 응답 자체는 schema-based fixture
    assert result.trace_id
    print("✅ test_summarize_with_query")


def test_summarize_invalid_capital(tmp_dir):
    agent = _setup_agent(tmp_dir)
    try:
        agent.summarize_market(
            starting_capital_krw="invalid",
            cash_krw="500000",
        )
        assert False
    except ValueError:
        pass
    print("✅ test_summarize_invalid_capital")


def test_summarize_negative_capital(tmp_dir):
    agent = _setup_agent(tmp_dir)
    try:
        agent.summarize_market(
            starting_capital_krw="-100",
            cash_krw="500000",
        )
        assert False
    except ValueError:
        pass
    print("✅ test_summarize_negative_capital")


# ─────────────────────────────────────────────────
# Schema 검증
# ─────────────────────────────────────────────────

def test_response_has_summary_ko(tmp_dir):
    agent = _setup_agent(tmp_dir)
    result = agent.summarize_market(
        starting_capital_krw="10000000",
        cash_krw="500000",
    )
    assert result.response is not None
    assert "summary_ko" in result.response
    print("✅ test_response_has_summary_ko")


def test_response_findings_list(tmp_dir):
    agent = _setup_agent(tmp_dir)
    result = agent.summarize_market(
        starting_capital_krw="10000000",
        cash_krw="500000",
    )
    assert "findings" in result.response
    assert isinstance(result.response["findings"], list)
    print("✅ test_response_findings_list")


# ─────────────────────────────────────────────────
# Fallback (LLM 실패 시)
# ─────────────────────────────────────────────────

def test_fallback_on_llm_parse_fail(tmp_dir):
    agent = _setup_agent(tmp_dir, llm_fail_mode="parse_fail")
    result = agent.summarize_market(
        starting_capital_krw="10000000",
        cash_krw="500000",
    )
    assert result.fallback_used
    assert result.success  # fallback도 success
    assert result.summary_ko  # fallback 응답에도 summary 있음
    assert "fallback" in (result.error or "").lower()
    print("✅ test_fallback_on_llm_parse_fail")


def test_fallback_on_llm_schema_fail(tmp_dir):
    agent = _setup_agent(tmp_dir, llm_fail_mode="schema_fail")
    result = agent.summarize_market(
        starting_capital_krw="10000000",
        cash_krw="500000",
    )
    assert result.fallback_used
    print("✅ test_fallback_on_llm_schema_fail")


def test_fallback_on_llm_exception(tmp_dir):
    agent = _setup_agent(tmp_dir, llm_fail_mode="exception")
    result = agent.summarize_market(
        starting_capital_krw="10000000",
        cash_krw="500000",
    )
    # exception은 LLMRequest 단계에서 처리되어 fallback으로
    assert result.fallback_used
    assert result.summary_ko
    print("✅ test_fallback_on_llm_exception")


def test_fallback_summary_contains_data(tmp_dir):
    """Fallback 응답에 도구 데이터가 반영됨."""
    agent = _setup_agent(tmp_dir, llm_fail_mode="parse_fail")
    result = agent.summarize_market(
        starting_capital_krw="10000000",
        cash_krw="500000",
    )
    # market_status 또는 positions 정보가 summary에 있어야 함
    summary = result.summary_ko.lower()
    assert any(kw in summary for kw in ["market", "포지션", "p&l", "krx"])
    print("✅ test_fallback_summary_contains_data")


# ─────────────────────────────────────────────────
# Audit 통합
# ─────────────────────────────────────────────────

def test_audit_full_trace(tmp_dir):
    """trace_id로 모든 이벤트 추적 가능."""
    agent = _setup_agent(tmp_dir)
    result = agent.summarize_market(
        starting_capital_krw="10000000",
        cash_krw="500000",
    )

    indexer = AuditIndexer(audit_dir=str(tmp_dir / "audit"))
    events = indexer.find_by_trace(result.trace_id)

    types = [e.event_type for e in events]
    # 주요 이벤트 모두 기록됨
    assert "mcp_tool_call" in types
    assert "mcp_tool_result" in types
    assert "agent_prompt" in types
    assert "agent_response" in types
    assert "agent_decision" in types
    print(f"✅ test_audit_full_trace ({len(events)} events)")


def test_audit_single_session_id(tmp_dir):
    """모든 audit 이벤트가 같은 session_id."""
    agent = _setup_agent(tmp_dir)
    result = agent.summarize_market(
        starting_capital_krw="10000000",
        cash_krw="500000",
    )

    indexer = AuditIndexer(audit_dir=str(tmp_dir / "audit"))
    events = indexer.find_by_trace(result.trace_id)
    session_ids = {e.session_id for e in events if e.session_id}
    assert len(session_ids) == 1
    assert "test-session" in session_ids
    print("✅ test_audit_single_session_id")


# ─────────────────────────────────────────────────
# Read-only 보장
# ─────────────────────────────────────────────────

def test_no_write_tools_called(tmp_dir):
    """audit에 write 이벤트가 절대 없어야 함."""
    agent = _setup_agent(tmp_dir)
    result = agent.summarize_market(
        starting_capital_krw="10000000",
        cash_krw="500000",
    )

    indexer = AuditIndexer(audit_dir=str(tmp_dir / "audit"))
    events = indexer.find_by_trace(result.trace_id)
    types = [e.event_type for e in events]

    # 절대 있으면 안 되는 이벤트
    forbidden = ("approval_request", "approval_decision",
                 "order_intent", "order_submitted")
    for t in forbidden:
        assert t not in types, \
            f"market_agent should not produce {t!r} events"
    print("✅ test_no_write_tools_called")


def test_mcp_client_only_readonly_tools(tmp_dir):
    """MarketAgent의 MCPReadOnlyClient는 read-only 8개 도구만."""
    agent = _setup_agent(tmp_dir)
    # MCPReadOnlyClient의 호출 가능 도구 확인
    mapping_tools = (
        "get_market_status", "get_positions", "get_pnl_snapshot",
        "get_recent_fills", "get_rejection_summary",
        "get_portfolio_risk", "get_strategy_registry", "get_trace",
    )
    for tool in mapping_tools:
        assert hasattr(agent.mcp_client, tool), f"missing {tool}"
    # write 도구는 method로 존재 안 함
    assert not hasattr(agent.mcp_client, "request_submit_order")
    assert not hasattr(agent.mcp_client, "execute_approved_action")
    print("✅ test_mcp_client_only_readonly_tools")


# ─────────────────────────────────────────────────
# 결과 메타데이터
# ─────────────────────────────────────────────────

def test_result_to_dict(tmp_dir):
    agent = _setup_agent(tmp_dir)
    result = agent.summarize_market(
        starting_capital_krw="10000000",
        cash_krw="500000",
    )
    d = result.to_dict()
    assert "trace_id" in d
    assert "summary_ko" in d
    assert "tool_calls_count" in d
    assert "llm_elapsed_ms" in d
    print("✅ test_result_to_dict")


def test_call_history_recorded(tmp_dir):
    """LLM call_history에 호출 기록."""
    agent = _setup_agent(tmp_dir)
    agent.summarize_market(
        starting_capital_krw="10000000",
        cash_krw="500000",
    )
    assert len(agent.llm_client.call_history) >= 1
    # 첫 호출에 system + user prompt 모두 있음
    req = agent.llm_client.call_history[0]
    assert "Market Analyst" in req.system_prompt
    assert "10000000" in req.user_prompt or "Tool Call Results" in req.user_prompt
    print("✅ test_call_history_recorded")


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
        test_summarize_market_basic, test_summarize_with_query,
        test_summarize_invalid_capital, test_summarize_negative_capital,
        test_response_has_summary_ko, test_response_findings_list,
        test_fallback_on_llm_parse_fail, test_fallback_on_llm_schema_fail,
        test_fallback_on_llm_exception, test_fallback_summary_contains_data,
        test_audit_full_trace, test_audit_single_session_id,
        test_no_write_tools_called, test_mcp_client_only_readonly_tools,
        test_result_to_dict, test_call_history_recorded,
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
    print("Task 37 v0.1 — market_agent 테스트")
    print("─" * 50)
    failed = _run_all()
    print("─" * 50)
    if failed == 0:
        print("✅ 모든 테스트 통과")
        sys.exit(0)
    else:
        print(f"❌ {failed}개 실패")
        sys.exit(1)
