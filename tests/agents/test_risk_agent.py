"""Task 38 — Risk Explanation Agent tests.

Tests verify:
    1. Frozen dataclass invariants (input/output validation)
    2. ActionCandidate safety guards (executed=False, approval=True)
    3. Tool collector ALLOWED_TOOLS enforcement
    4. Severity aggregation correctness
    5. Action candidate routing + dedup
    6. Fallback builder produces schema-conforming output
    7. Agent end-to-end with mock LLM + mock MCP

Stubs are defined inline to keep this test file self-contained — no
dependency on Task 37 modules being installed in the test environment.
"""
from __future__ import annotations

import pytest
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock


# =============================================================================
# Inline stubs — substitute for Task 37 modules during isolated unit tests
# =============================================================================

@dataclass(frozen=True)
class _StubMCPCallResult:
    tool_name: str
    success: bool
    data: dict
    trace_id: str = "test-trace"
    elapsed_ms: int = 0


class _StubMCPReadOnlyClient:
    """In-memory MCP read-only client stub. Returns canned tool responses."""

    def __init__(self, responses: dict[str, dict] | None = None,
                 fail_tools: set[str] | None = None) -> None:
        self.responses = responses or {}
        self.fail_tools = fail_tools or set()
        self.calls: list[tuple[str, dict]] = []

    def call(self, tool_name: str, **kwargs: Any) -> _StubMCPCallResult:
        self.calls.append((tool_name, kwargs))
        if tool_name in self.fail_tools:
            raise RuntimeError(f"simulated failure for {tool_name}")
        data = self.responses.get(tool_name, {})
        return _StubMCPCallResult(
            tool_name=tool_name,
            success=True,
            data=data,
        )


@dataclass(frozen=True)
class _StubAgentRunResult:
    trace_id: str
    payload: dict
    raw_llm_response: str
    fallback_used: bool
    schema_validated: bool
    elapsed_ms: int


@dataclass(frozen=True)
class _StubAgentSpec:
    agent_name: str
    system_template_id: str
    user_template_id: str
    schema_id: str
    tool_collector: Any
    fallback_builder: Any
    max_tool_calls: int


class _StubAgentRunner:
    """Stub runner — returns whatever payload was set on the fixture."""

    def __init__(self, llm_client, mcp_client, audit_writer, spec) -> None:
        self.llm = llm_client
        self.mcp = mcp_client
        self.audit = audit_writer
        self.spec = spec
        self.next_payload: dict | None = None
        self.next_fallback_used: bool = False
        self.next_schema_validated: bool = True

    def run(self, *, template_variables: dict, parent_trace=None) -> _StubAgentRunResult:
        # Always invoke tool_collector to verify it works
        tool_results = self.spec.tool_collector(parent_trace=parent_trace)
        if self.next_payload is None:
            # Use fallback to derive payload
            payload = self.spec.fallback_builder(
                tool_results, template_variables.get("operator_query", "")
            )
            fb_used = True
        else:
            payload = self.next_payload
            fb_used = self.next_fallback_used
        return _StubAgentRunResult(
            trace_id="test-trace-123",
            payload=payload,
            raw_llm_response="(stub)",
            fallback_used=fb_used,
            schema_validated=self.next_schema_validated,
            elapsed_ms=42,
        )


# Patch module-level imports BEFORE importing risk_agent
import sys
import types

_agents_pkg = types.ModuleType("test_agents_pkg")
sys.modules.setdefault("test_agents_pkg", _agents_pkg)

# Inject stub modules so risk_agent's `from ._xxx import ...` works
_runner_mod = types.ModuleType("test_agents_pkg._agent_runner")
_runner_mod.AgentRunner = _StubAgentRunner
_runner_mod.AgentSpec = _StubAgentSpec
_runner_mod.AgentRunResult = _StubAgentRunResult
sys.modules["test_agents_pkg._agent_runner"] = _runner_mod

_llm_mod = types.ModuleType("test_agents_pkg._llm_client")
_llm_mod.LLMClient = type("LLMClient", (), {})
sys.modules["test_agents_pkg._llm_client"] = _llm_mod

_mcp_mod = types.ModuleType("test_agents_pkg._mcp_client")
_mcp_mod.MCPReadOnlyClient = _StubMCPReadOnlyClient
_mcp_mod.MCPCallResult = _StubMCPCallResult
sys.modules["test_agents_pkg._mcp_client"] = _mcp_mod

# Now load risk_agent under the test_agents_pkg namespace
import importlib.util
import os

_risk_agent_path = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "..", "src", "agents", "risk_agent.py",
)
_risk_agent_path = os.path.normpath(_risk_agent_path)

_spec = importlib.util.spec_from_file_location(
    "test_agents_pkg.risk_agent",
    _risk_agent_path,
)
risk_agent = importlib.util.module_from_spec(_spec)
sys.modules["test_agents_pkg.risk_agent"] = risk_agent
_spec.loader.exec_module(risk_agent)

# Pull names for convenience
RiskExplanationAgent = risk_agent.RiskExplanationAgent
RiskAgentInput = risk_agent.RiskAgentInput
RiskAgentReport = risk_agent.RiskAgentReport
ActionCandidate = risk_agent.ActionCandidate
RiskAgentError = risk_agent.RiskAgentError
SEVERITY_LEVELS = risk_agent.SEVERITY_LEVELS
SUGGESTABLE_WRITE_TOOLS = risk_agent.SUGGESTABLE_WRITE_TOOLS
ALLOWED_TOOLS = risk_agent.ALLOWED_TOOLS
_collect_risk_tools = risk_agent._collect_risk_tools
_build_action_candidates = risk_agent._build_action_candidates
_build_risk_fallback = risk_agent._build_risk_fallback
_max_severity = risk_agent._max_severity
_classify_breach = risk_agent._classify_breach
_extract_breaches_from_tools = risk_agent._extract_breaches_from_tools


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


@pytest.fixture
def mcp_normal() -> _StubMCPReadOnlyClient:
    """Normal MCP client — no breaches, no rejections."""
    return _StubMCPReadOnlyClient(responses={
        "get_market_status": {"is_open": True, "session": "regular"},
        "get_portfolio_risk": {"breaches": []},
        "get_rejection_summary": {"count_24h": 0, "recent_rejections": []},
        "get_positions": {"positions": []},
        "get_pnl_snapshot": {"realized": "0", "unrealized": "0"},
    })


@pytest.fixture
def mcp_with_breach() -> _StubMCPReadOnlyClient:
    """MCP returning a position-limit breach + 8 rejections."""
    return _StubMCPReadOnlyClient(responses={
        "get_market_status": {"is_open": True, "session": "regular"},
        "get_portfolio_risk": {
            "breaches": [{
                "type": "position_limit_breach",
                "severity": "high",
                "ratio": "1.6",
                "symbol": "005930",
                "description_kr": "포지션 한도 초과 — 005930",
            }],
        },
        "get_rejection_summary": {"count_24h": 8, "recent_rejections": [
            {"reason_code": "position_limit", "symbol": "005930",
             "client_order_id": "ord-1", "strategy_id": "momentum_v1"},
        ]},
        "get_positions": {"positions": [{"symbol": "005930", "qty": "1000"}]},
        "get_pnl_snapshot": {"realized": "-50000", "unrealized": "10000"},
    })


@pytest.fixture
def llm_stub() -> MagicMock:
    """Stub LLM client — methods unused since AgentRunner is stubbed."""
    m = MagicMock()
    m.model_id = "stub-llm"
    return m


# =============================================================================
# Tests — RiskAgentInput validation
# =============================================================================

class TestRiskAgentInput:
    def test_accepts_valid_input(self, utc_now):
        inp = RiskAgentInput(
            starting_capital_krw=Decimal("100000000"),
            current_cash_krw=Decimal("80000000"),
            operator_query="현재 위험 상태",
            requested_at_utc=utc_now,
        )
        assert inp.starting_capital_krw == Decimal("100000000")

    def test_rejects_non_decimal_capital(self, utc_now):
        with pytest.raises(TypeError, match="must be Decimal"):
            RiskAgentInput(
                starting_capital_krw=100000000,  # int, not Decimal
                current_cash_krw=Decimal("80000000"),
                operator_query="x",
                requested_at_utc=utc_now,
            )

    def test_rejects_negative_capital(self, utc_now):
        with pytest.raises(ValueError, match="must be positive"):
            RiskAgentInput(
                starting_capital_krw=Decimal("-1"),
                current_cash_krw=Decimal("0"),
                operator_query="x",
                requested_at_utc=utc_now,
            )

    def test_rejects_empty_query(self, utc_now):
        with pytest.raises(ValueError, match="non-empty"):
            RiskAgentInput(
                starting_capital_krw=Decimal("1"),
                current_cash_krw=Decimal("0"),
                operator_query="   ",
                requested_at_utc=utc_now,
            )

    def test_rejects_naive_datetime(self):
        with pytest.raises(ValueError, match="tz-aware"):
            RiskAgentInput(
                starting_capital_krw=Decimal("1"),
                current_cash_krw=Decimal("0"),
                operator_query="x",
                requested_at_utc=datetime(2026, 5, 7),  # naive
            )

    def test_input_is_frozen(self, utc_now):
        inp = RiskAgentInput(
            starting_capital_krw=Decimal("1"),
            current_cash_krw=Decimal("0"),
            operator_query="x",
            requested_at_utc=utc_now,
        )
        with pytest.raises((AttributeError, Exception)):
            inp.operator_query = "tampered"  # type: ignore


# =============================================================================
# Tests — ActionCandidate safety guards
# =============================================================================

class TestActionCandidate:
    def test_accepts_valid_candidate(self):
        c = ActionCandidate(
            tool_name="request_cancel_order",
            rationale_kr="테스트",
            parameters_preview={"order_id": "x"},
            severity="high",
        )
        assert c.executed is False
        assert c.requires_human_approval is True

    def test_rejects_unauthorized_tool(self):
        with pytest.raises(RiskAgentError, match="not in suggestable allowlist"):
            ActionCandidate(
                tool_name="approve_action",  # internal-only
                rationale_kr="x",
                parameters_preview={},
                severity="high",
            )

    def test_rejects_executed_true(self):
        with pytest.raises(RiskAgentError, match="executed must be False"):
            ActionCandidate(
                tool_name="request_cancel_order",
                rationale_kr="x",
                parameters_preview={},
                severity="high",
                executed=True,
            )

    def test_rejects_approval_false(self):
        with pytest.raises(RiskAgentError, match="requires_human_approval"):
            ActionCandidate(
                tool_name="request_cancel_order",
                rationale_kr="x",
                parameters_preview={},
                severity="high",
                requires_human_approval=False,
            )

    def test_rejects_invalid_severity(self):
        with pytest.raises(ValueError, match="severity must be one of"):
            ActionCandidate(
                tool_name="request_cancel_order",
                rationale_kr="x",
                parameters_preview={},
                severity="catastrophic",
            )


# =============================================================================
# Tests — Tool collector
# =============================================================================

class TestToolCollector:
    def test_collects_all_five_tools_in_order(self, mcp_normal):
        results = _collect_risk_tools(mcp_normal)
        assert len(results) == 5
        expected_order = [
            "get_market_status",
            "get_portfolio_risk",
            "get_rejection_summary",
            "get_positions",
            "get_pnl_snapshot",
        ]
        assert [r.tool_name for r in results] == expected_order

    def test_failed_call_does_not_abort_chain(self):
        mcp = _StubMCPReadOnlyClient(
            responses={
                "get_market_status": {"is_open": True},
                "get_portfolio_risk": {"breaches": []},
                "get_positions": {"positions": []},
                "get_pnl_snapshot": {},
            },
            fail_tools={"get_rejection_summary"},
        )
        results = _collect_risk_tools(mcp)
        assert len(results) == 5
        rejection_result = results[2]
        assert rejection_result.tool_name == "get_rejection_summary"
        assert rejection_result.success is False
        assert "error" in rejection_result.data


# =============================================================================
# Tests — Severity logic
# =============================================================================

class TestSeverity:
    def test_max_severity_picks_highest(self):
        assert _max_severity(["info", "high", "low"]) == "high"

    def test_max_severity_handles_unknown(self):
        assert _max_severity(["unknown", "medium"]) == "medium"

    def test_max_severity_empty_iter(self):
        assert _max_severity([]) == "info"

    def test_classify_breach_thresholds(self):
        assert _classify_breach("x", Decimal("0.9")) == "info"
        assert _classify_breach("x", Decimal("1.0")) == "low"
        assert _classify_breach("x", Decimal("1.3")) == "medium"
        assert _classify_breach("x", Decimal("1.5")) == "high"
        assert _classify_breach("x", Decimal("2.0")) == "critical"


# =============================================================================
# Tests — Action candidate building
# =============================================================================

class TestActionCandidateBuilder:
    def test_position_limit_breach_routes_to_flatten(self):
        breaches = [{
            "type": "position_limit_breach",
            "severity": "high",
            "symbol": "005930",
            "description_kr": "x",
        }]
        cands = _build_action_candidates(breaches, rejection_count=0)
        tool_names = [c.tool_name for c in cands]
        assert "request_flatten_position" in tool_names

    def test_dedups_repeated_tools(self):
        breaches = [
            {"type": "position_limit_breach", "severity": "high", "symbol": "A"},
            {"type": "concentration_breach", "severity": "medium", "symbol": "B"},
        ]
        cands = _build_action_candidates(breaches, rejection_count=0)
        tool_names = [c.tool_name for c in cands]
        # request_flatten_position appears in both routings; should appear once
        assert tool_names.count("request_flatten_position") == 1

    def test_max_candidates_capped(self):
        breaches = [
            {"type": "position_limit_breach", "severity": "high", "symbol": f"S{i}"}
            for i in range(20)
        ]
        cands = _build_action_candidates(breaches, rejection_count=50)
        assert len(cands) <= 5

    def test_rejection_spike_adds_pause(self):
        cands = _build_action_candidates([], rejection_count=10)
        tool_names = [c.tool_name for c in cands]
        assert "request_strategy_pause" in tool_names

    def test_no_breach_no_rejections_yields_empty(self):
        cands = _build_action_candidates([], rejection_count=0)
        assert cands == ()


# =============================================================================
# Tests — Fallback builder
# =============================================================================

class TestFallbackBuilder:
    def test_no_breach_returns_normal_summary(self, mcp_normal):
        results = _collect_risk_tools(mcp_normal)
        payload = _build_risk_fallback(results, "현재 상태")
        assert payload["fallback_used"] is True
        assert payload["breach_count"] == 0
        assert payload["severity_overall"] == "info"
        assert "정상 운영" in payload["summary_kr"]

    def test_with_breach_returns_high_severity(self, mcp_with_breach):
        results = _collect_risk_tools(mcp_with_breach)
        payload = _build_risk_fallback(results, "위험 분석")
        assert payload["breach_count"] >= 1
        assert payload["severity_overall"] in ("high", "critical")
        assert payload["rejection_count_24h"] == 8
        # Action candidates should include request_flatten_position
        tools = [c["tool_name"] for c in payload["action_candidates"]]
        assert "request_flatten_position" in tools

    def test_fallback_payload_has_required_keys(self, mcp_normal):
        results = _collect_risk_tools(mcp_normal)
        payload = _build_risk_fallback(results, "x")
        required = {
            "summary_kr", "severity_overall", "breach_count",
            "rejection_count_24h", "breaches", "action_candidates",
            "operator_query_echo", "fallback_used",
        }
        assert required.issubset(payload.keys())

    def test_extract_breaches_from_rejection_pattern(self):
        results = (
            _StubMCPCallResult("get_market_status", True, {}),
            _StubMCPCallResult("get_portfolio_risk", True, {"breaches": []}),
            _StubMCPCallResult("get_rejection_summary", True, {
                "count_24h": 3,
                "recent_rejections": [
                    {"reason_code": "position_limit", "symbol": "X",
                     "client_order_id": "o1", "strategy_id": "s1"},
                ],
            }),
            _StubMCPCallResult("get_positions", True, {}),
            _StubMCPCallResult("get_pnl_snapshot", True, {}),
        )
        breaches, count = _extract_breaches_from_tools(results)
        assert count == 3
        assert len(breaches) == 1
        assert breaches[0]["type"] == "position_limit_breach"


# =============================================================================
# Tests — Agent end-to-end
# =============================================================================

class TestRiskExplanationAgent:
    def test_agent_runs_with_normal_state(self, llm_stub, mcp_normal, utc_now):
        agent = RiskExplanationAgent(llm_stub, mcp_normal)
        report = agent.explain_risk(
            starting_capital_krw=Decimal("100000000"),
            current_cash_krw=Decimal("100000000"),
            operator_query="현재 상태 점검",
        )
        assert isinstance(report, RiskAgentReport)
        assert report.severity_overall == "info"
        assert report.breach_count == 0
        assert report.action_candidates == ()

    def test_agent_runs_with_breach_state(self, llm_stub, mcp_with_breach, utc_now):
        agent = RiskExplanationAgent(llm_stub, mcp_with_breach)
        report = agent.explain_risk(
            starting_capital_krw=Decimal("100000000"),
            current_cash_krw=Decimal("80000000"),
            operator_query="위험 분석 요청",
        )
        assert report.breach_count >= 1
        assert report.severity_overall in ("high", "critical")
        assert len(report.action_candidates) >= 1
        # Defense in depth — every candidate must have safety flags set
        for c in report.action_candidates:
            assert c.executed is False
            assert c.requires_human_approval is True
            assert c.tool_name in SUGGESTABLE_WRITE_TOOLS

    def test_agent_drops_unauthorized_tool_in_payload(self, llm_stub, mcp_normal):
        """If LLM hallucinates a non-suggestable tool, agent silently drops it."""
        agent = RiskExplanationAgent(llm_stub, mcp_normal)
        # Inject a payload via the stub runner
        agent._runner.next_payload = {
            "summary_kr": "테스트",
            "severity_overall": "high",
            "breach_count": 1,
            "rejection_count_24h": 0,
            "action_candidates": [
                {"tool_name": "approve_action",  # forbidden — internal only
                 "rationale_kr": "x", "parameters_preview": {}, "severity": "high"},
                {"tool_name": "request_cancel_order",  # OK
                 "rationale_kr": "y", "parameters_preview": {"order_id": "o1"},
                 "severity": "high"},
            ],
        }
        agent._runner.next_fallback_used = False
        report = agent.explain_risk(
            starting_capital_krw=Decimal("1000000"),
            current_cash_krw=Decimal("500000"),
            operator_query="test",
        )
        assert len(report.action_candidates) == 1
        assert report.action_candidates[0].tool_name == "request_cancel_order"

    def test_agent_rejects_invalid_max_tool_calls(self, llm_stub, mcp_normal):
        with pytest.raises(ValueError, match="max_tool_calls"):
            RiskExplanationAgent(llm_stub, mcp_normal, max_tool_calls=0)
        with pytest.raises(ValueError, match="max_tool_calls"):
            RiskExplanationAgent(llm_stub, mcp_normal, max_tool_calls=999)

    def test_agent_rejects_none_clients(self, mcp_normal, llm_stub):
        with pytest.raises(ValueError, match="llm_client"):
            RiskExplanationAgent(None, mcp_normal)
        with pytest.raises(ValueError, match="mcp_client"):
            RiskExplanationAgent(llm_stub, None)

    def test_agent_audit_failure_does_not_crash(self, llm_stub, mcp_normal):
        """Audit failures must never propagate."""
        broken_audit = MagicMock()
        broken_audit.write_event.side_effect = RuntimeError("audit dead")
        agent = RiskExplanationAgent(llm_stub, mcp_normal, audit_writer=broken_audit)
        # Should not raise
        report = agent.explain_risk(
            starting_capital_krw=Decimal("1"),
            current_cash_krw=Decimal("0"),
            operator_query="x",
        )
        assert isinstance(report, RiskAgentReport)


# =============================================================================
# Tests — Allowlist invariants (hard security checks)
# =============================================================================

class TestAllowlists:
    def test_allowed_tools_contains_only_read_only(self):
        for tool in ALLOWED_TOOLS:
            assert tool.startswith("get_"), f"{tool} is not read-only"

    def test_suggestable_write_tools_contains_only_request(self):
        for tool in SUGGESTABLE_WRITE_TOOLS:
            assert tool.startswith("request_"), f"{tool} is not a request tool"

    def test_no_overlap_between_allowed_and_suggestable(self):
        assert ALLOWED_TOOLS.isdisjoint(SUGGESTABLE_WRITE_TOOLS)

    def test_internal_approval_tools_not_suggestable(self):
        forbidden = {"approve_action", "reject_action",
                     "execute_approved_action"}
        assert forbidden.isdisjoint(SUGGESTABLE_WRITE_TOOLS)
