"""
스모크 테스트 — 실제 템플릿 로드 (Task 36)
==========================================

src/agents/prompts/ 의 실제 .md 파일 로드 + 검증.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from src.agents.prompts import (  # noqa: E402
    AGENT_COMMON,
    AGENT_MARKET_ANALYST,
    AGENT_PNL_EXPLAINER,
    AGENT_RISK_EXPLAINER,
    PromptRegistry,
    safe_render,
)


def _registry() -> PromptRegistry:
    return PromptRegistry()  # uses DEFAULT_PROMPT_ROOT


# ─────────────────────────────────────────────────
# System prompts (3개)
# ─────────────────────────────────────────────────

def test_market_analyst_system_loaded():
    reg = _registry()
    tmpl = reg.get("market_analyst.system")
    assert tmpl.role == "system"
    assert tmpl.target_agent == AGENT_MARKET_ANALYST
    assert tmpl.response_schema is not None
    assert "session_id" in tmpl.required_variables
    assert "operator_id" in tmpl.required_variables
    print("✅ test_market_analyst_system_loaded")


def test_risk_explainer_system_loaded():
    reg = _registry()
    tmpl = reg.get("risk_explainer.system")
    assert tmpl.target_agent == AGENT_RISK_EXPLAINER
    assert tmpl.response_schema is not None
    print("✅ test_risk_explainer_system_loaded")


def test_pnl_explainer_system_loaded():
    reg = _registry()
    tmpl = reg.get("pnl_explainer.system")
    assert tmpl.target_agent == AGENT_PNL_EXPLAINER
    assert tmpl.response_schema is not None
    print("✅ test_pnl_explainer_system_loaded")


# ─────────────────────────────────────────────────
# Tool guides (2개)
# ─────────────────────────────────────────────────

def test_readonly_tools_guide_loaded():
    reg = _registry()
    tmpl = reg.get("common.readonly_tools")
    assert tmpl.role == "tool_guide"
    assert tmpl.target_agent == AGENT_COMMON
    # 모든 8개 도구 이름 포함 확인
    for tool in [
        "get_market_status", "get_positions", "get_pnl_snapshot",
        "get_recent_fills", "get_rejection_summary",
        "get_portfolio_risk", "get_strategy_registry", "get_trace",
    ]:
        assert tool in tmpl.body, f"missing tool {tool}"
    print("✅ test_readonly_tools_guide_loaded")


def test_restricted_tools_guide_loaded():
    reg = _registry()
    tmpl = reg.get("common.restricted_tools")
    assert tmpl.target_agent == AGENT_COMMON
    for tool in [
        "request_submit_order", "request_cancel_order",
        "request_set_capacity", "request_kill_switch",
        "list_pending_approvals", "get_approval_status",
        "cancel_request", "execute_approved_action",
    ]:
        assert tool in tmpl.body, f"missing tool {tool}"
    print("✅ test_restricted_tools_guide_loaded")


# ─────────────────────────────────────────────────
# User tasks (3개)
# ─────────────────────────────────────────────────

def test_market_summary_task_loaded():
    reg = _registry()
    tmpl = reg.get("market_analyst.market_summary")
    assert tmpl.role == "user"
    assert "starting_capital_krw" in tmpl.required_variables
    assert "cash_krw" in tmpl.required_variables
    print("✅ test_market_summary_task_loaded")


def test_risk_breach_task_loaded():
    reg = _registry()
    tmpl = reg.get("risk_explainer.risk_breach_explain")
    assert "trace_id" in tmpl.required_variables
    print("✅ test_risk_breach_task_loaded")


def test_pnl_attribution_task_loaded():
    reg = _registry()
    tmpl = reg.get("pnl_explainer.pnl_attribution")
    assert "starting_capital_krw" in tmpl.required_variables
    assert "since_iso" in tmpl.required_variables
    print("✅ test_pnl_attribution_task_loaded")


# ─────────────────────────────────────────────────
# 일괄 로드
# ─────────────────────────────────────────────────

def test_list_all_no_errors():
    reg = _registry()
    all_tmpls = reg.list_all()
    # 최소 8개 (system 3 + tool_guide 2 + user 3)
    assert len(all_tmpls) >= 8, f"got {len(all_tmpls)}"
    print(f"✅ test_list_all_no_errors ({len(all_tmpls)} templates)")


def test_unique_template_ids():
    reg = _registry()
    ids = [t.template_id for t in reg.list_all()]
    assert len(ids) == len(set(ids)), "duplicate ids"
    print("✅ test_unique_template_ids")


# ─────────────────────────────────────────────────
# 렌더링 통합 (체결)
# ─────────────────────────────────────────────────

def test_market_analyst_system_render():
    reg = _registry()
    tmpl = reg.get("market_analyst.system")
    rp = safe_render(tmpl, {
        "session_id": "test-session-2026",
        "operator_id": "alice",
    })
    assert "test-session-2026" in rp.rendered_text
    assert "alice" in rp.rendered_text
    assert "{{" not in rp.rendered_text  # 모든 자리표시자 치환됨
    print("✅ test_market_analyst_system_render")


def test_market_summary_render():
    reg = _registry()
    tmpl = reg.get("market_analyst.market_summary")
    rp = safe_render(tmpl, {
        "starting_capital_krw": "10000000",
        "cash_krw": "500000",
    })
    assert "10000000" in rp.rendered_text
    assert "500000" in rp.rendered_text
    print("✅ test_market_summary_render")


def test_risk_breach_render():
    reg = _registry()
    tmpl = reg.get("risk_explainer.risk_breach_explain")
    rp = safe_render(tmpl, {
        "trace_id": "trc-20260507-deadbeef",
    })
    assert "trc-20260507-deadbeef" in rp.rendered_text
    print("✅ test_risk_breach_render")


def test_pnl_attribution_render():
    reg = _registry()
    tmpl = reg.get("pnl_explainer.pnl_attribution")
    rp = safe_render(tmpl, {
        "starting_capital_krw": "10000000",
        "cash_krw": "500000",
        "since_iso": "2026-05-07T00:00:00Z",
    })
    assert "10000000" in rp.rendered_text
    assert "2026-05-07" in rp.rendered_text
    print("✅ test_pnl_attribution_render")


# ─────────────────────────────────────────────────
# Schema 검증 (response_schema 형식)
# ─────────────────────────────────────────────────

def test_market_schema_structure():
    reg = _registry()
    tmpl = reg.get("market_analyst.system")
    s = tmpl.response_schema
    assert s["type"] == "object"
    assert "summary_ko" in s["properties"]
    assert "findings" in s["properties"]
    print("✅ test_market_schema_structure")


def test_risk_schema_structure():
    reg = _registry()
    tmpl = reg.get("risk_explainer.system")
    s = tmpl.response_schema
    assert "severity" in s["properties"]
    assert "evidence" in s["properties"]
    print("✅ test_risk_schema_structure")


def test_pnl_schema_structure():
    reg = _registry()
    tmpl = reg.get("pnl_explainer.system")
    s = tmpl.response_schema
    assert "total_pnl_krw" in s["properties"]
    assert "by_strategy" in s["properties"]
    print("✅ test_pnl_schema_structure")


# ─────────────────────────────────────────────────
# 실행
# ─────────────────────────────────────────────────

def _run_all() -> int:
    failed = 0
    tests = [
        test_market_analyst_system_loaded,
        test_risk_explainer_system_loaded,
        test_pnl_explainer_system_loaded,
        test_readonly_tools_guide_loaded,
        test_restricted_tools_guide_loaded,
        test_market_summary_task_loaded,
        test_risk_breach_task_loaded,
        test_pnl_attribution_task_loaded,
        test_list_all_no_errors,
        test_unique_template_ids,
        test_market_analyst_system_render,
        test_market_summary_render,
        test_risk_breach_render,
        test_pnl_attribution_render,
        test_market_schema_structure,
        test_risk_schema_structure,
        test_pnl_schema_structure,
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
    print("Task 36 v0.1 — 로드된 템플릿 통합 테스트")
    print("─" * 50)
    failed = _run_all()
    print("─" * 50)
    if failed == 0:
        print("✅ 모든 테스트 통과")
        sys.exit(0)
    else:
        print(f"❌ {failed}개 실패")
        sys.exit(1)
