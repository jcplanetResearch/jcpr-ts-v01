"""Task 39 — P&L Explanation Agent tests.

Tests verify:
    1. PnLAgentInput frozen dataclass invariants
    2. PnLDecomposition / AttributionRow validation
    3. ALLOWED_TOOLS enforcement (no write tools)
    4. _to_decimal robust conversion
    5. _mask_order_id PII protection
    6. _classify_materiality threshold mapping
    7. Decomposition extraction from snapshot + slippage compute from fills
    8. Strategy / symbol attribution aggregation + sorting
    9. Notable events extraction with order_id masking
   10. Fallback builder produces schema-valid payload
   11. End-to-end agent run with mocked LLM + MCP

Stubs are inline — no dependency on Task 37 modules being installed.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest


# =============================================================================
# Inline stubs — substitute for Task 37 modules
# =============================================================================

@dataclass(frozen=True)
class _StubMCPCallResult:
    tool_name: str
    success: bool
    data: dict
    trace_id: str = "test-trace"
    elapsed_ms: int = 0


class _StubMCPReadOnlyClient:
    def __init__(self, responses: dict[str, dict] | None = None,
                 fail_tools: set[str] | None = None) -> None:
        self.responses = responses or {}
        self.fail_tools = fail_tools or set()
        self.calls: list[tuple[str, dict]] = []

    def call(self, tool_name: str, **kwargs: Any) -> _StubMCPCallResult:
        self.calls.append((tool_name, kwargs))
        if tool_name in self.fail_tools:
            raise RuntimeError(f"simulated failure for {tool_name}")
        return _StubMCPCallResult(
            tool_name=tool_name,
            success=True,
            data=self.responses.get(tool_name, {}),
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
    def __init__(self, llm_client, mcp_client, audit_writer, spec) -> None:
        self.llm = llm_client
        self.mcp = mcp_client
        self.audit = audit_writer
        self.spec = spec
        self.next_payload: dict | None = None
        self.next_fallback_used: bool = False
        self.next_schema_validated: bool = True

    def run(self, *, template_variables: dict, parent_trace=None) -> _StubAgentRunResult:
        tool_results = self.spec.tool_collector(parent_trace=parent_trace)
        if self.next_payload is None:
            payload = self.spec.fallback_builder(
                tool_results, template_variables.get("operator_query", "")
            )
            fb_used = True
        else:
            payload = self.next_payload
            fb_used = self.next_fallback_used
        return _StubAgentRunResult(
            trace_id="test-trace-pnl",
            payload=payload,
            raw_llm_response="(stub)",
            fallback_used=fb_used,
            schema_validated=self.next_schema_validated,
            elapsed_ms=42,
        )


# Inject stubs as if they were Task 37 modules
_pkg = types.ModuleType("test_agents_pkg39")
sys.modules["test_agents_pkg39"] = _pkg

_runner_mod = types.ModuleType("test_agents_pkg39._agent_runner")
_runner_mod.AgentRunner = _StubAgentRunner
_runner_mod.AgentSpec = _StubAgentSpec
_runner_mod.AgentRunResult = _StubAgentRunResult
sys.modules["test_agents_pkg39._agent_runner"] = _runner_mod

_llm_mod = types.ModuleType("test_agents_pkg39._llm_client")
_llm_mod.LLMClient = type("LLMClient", (), {})
sys.modules["test_agents_pkg39._llm_client"] = _llm_mod

_mcp_mod = types.ModuleType("test_agents_pkg39._mcp_client")
_mcp_mod.MCPReadOnlyClient = _StubMCPReadOnlyClient
_mcp_mod.MCPCallResult = _StubMCPCallResult
sys.modules["test_agents_pkg39._mcp_client"] = _mcp_mod

# Load pnl_agent under the test package namespace
_pnl_path = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "..", "src", "agents", "pnl_agent.py",
))
_spec = importlib.util.spec_from_file_location(
    "test_agents_pkg39.pnl_agent", _pnl_path,
)
pnl_agent = importlib.util.module_from_spec(_spec)
sys.modules["test_agents_pkg39.pnl_agent"] = pnl_agent
_spec.loader.exec_module(pnl_agent)

# Pull names
PnLExplanationAgent = pnl_agent.PnLExplanationAgent
PnLAgentInput = pnl_agent.PnLAgentInput
PnLAgentReport = pnl_agent.PnLAgentReport
PnLDecomposition = pnl_agent.PnLDecomposition
AttributionRow = pnl_agent.AttributionRow
PnLAgentError = pnl_agent.PnLAgentError
ALLOWED_TOOLS = pnl_agent.ALLOWED_TOOLS
MATERIALITY_HIGH_KRW = pnl_agent.MATERIALITY_HIGH_KRW
MATERIALITY_MEDIUM_KRW = pnl_agent.MATERIALITY_MEDIUM_KRW
MATERIALITY_LOW_KRW = pnl_agent.MATERIALITY_LOW_KRW
_to_decimal = pnl_agent._to_decimal
_mask_order_id = pnl_agent._mask_order_id
_classify_materiality = pnl_agent._classify_materiality
_collect_pnl_tools = pnl_agent._collect_pnl_tools
_extract_decomposition = pnl_agent._extract_decomposition
_build_attribution = pnl_agent._build_attribution
_extract_notable_events = pnl_agent._extract_notable_events
_build_pnl_fallback = pnl_agent._build_pnl_fallback


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


@pytest.fixture
def mcp_normal_pnl() -> _StubMCPReadOnlyClient:
    """MCP returning small profit, mixed strategies/symbols."""
    return _StubMCPReadOnlyClient(responses={
        "get_market_status": {"is_open": False, "session": "after_hours"},
        "get_pnl_snapshot": {
            "realized_krw": "150000",
            "unrealized_krw": "50000",
            "fees_krw": "5000",
            "slippage_krw": "8000",
            "notable_events": [],
        },
        "get_recent_fills": {"fills": [
            {"strategy_id": "momentum_v1", "symbol": "005930",
             "realized_pnl_krw": "100000", "side": "buy",
             "arrival_price_krw": "70000", "fill_price_krw": "70100", "qty": "10"},
            {"strategy_id": "momentum_v1", "symbol": "000660",
             "realized_pnl_krw": "50000", "side": "sell",
             "arrival_price_krw": "150000", "fill_price_krw": "149800", "qty": "5"},
        ]},
        "get_positions": {"positions": [
            {"strategy_id": "momentum_v1", "symbol": "035420",
             "unrealized_pnl_krw": "50000"},
        ]},
        "get_strategy_registry": {"strategies": [{"id": "momentum_v1"}]},
    })


@pytest.fixture
def mcp_loss_pnl() -> _StubMCPReadOnlyClient:
    """MCP returning material loss with notable events."""
    return _StubMCPReadOnlyClient(responses={
        "get_market_status": {"is_open": True},
        "get_pnl_snapshot": {
            "realized_krw": "-2000000",
            "unrealized_krw": "-500000",
            "fees_krw": "30000",
            "slippage_krw": "100000",
            "notable_events": [
                {"event_type": "stop_loss_hit",
                 "timestamp_utc": "2026-05-07T09:30:00Z",
                 "description_kr": "손절(stop loss) 발동 — 005930",
                 "impact_krw": "-1500000",
                 "order_id": "order-abc-1234567890XYZ"},
            ],
        },
        "get_recent_fills": {"fills": [
            {"strategy_id": "mean_revert_v1", "symbol": "005930",
             "realized_pnl_krw": "-1500000", "side": "sell",
             "arrival_price_krw": "70000", "fill_price_krw": "69500", "qty": "100"},
            {"strategy_id": "mean_revert_v1", "symbol": "069500",
             "realized_pnl_krw": "-500000", "side": "buy",
             "arrival_price_krw": "30000", "fill_price_krw": "30200", "qty": "50"},
        ]},
        "get_positions": {"positions": [
            {"strategy_id": "mean_revert_v1", "symbol": "069500",
             "unrealized_pnl_krw": "-500000"},
        ]},
        "get_strategy_registry": {"strategies": []},
    })


@pytest.fixture
def llm_stub() -> MagicMock:
    m = MagicMock()
    m.model_id = "stub-llm"
    return m


# =============================================================================
# Tests — _to_decimal
# =============================================================================

class TestToDecimal:
    def test_passes_through_decimal(self):
        assert _to_decimal(Decimal("123.45")) == Decimal("123.45")

    def test_converts_string(self):
        assert _to_decimal("100") == Decimal("100")

    def test_converts_int(self):
        assert _to_decimal(42) == Decimal("42")

    def test_returns_default_on_none(self):
        assert _to_decimal(None) == Decimal("0")
        assert _to_decimal(None, default=Decimal("99")) == Decimal("99")

    def test_returns_default_on_garbage(self):
        assert _to_decimal("not a number") == Decimal("0")
        assert _to_decimal({}) == Decimal("0")


# =============================================================================
# Tests — _mask_order_id (PII protection)
# =============================================================================

class TestMaskOrderId:
    def test_short_id_returned_as_is(self):
        assert _mask_order_id("ord-1") == "ord-1"

    def test_long_id_truncated(self):
        result = _mask_order_id("order-abc-1234567890XYZ")
        assert result.endswith("...")
        assert len(result) <= 12  # 8 chars + "..."
        assert "1234567890XYZ" not in result

    def test_empty_returns_empty(self):
        assert _mask_order_id("") == ""
        assert _mask_order_id(None) == ""


# =============================================================================
# Tests — _classify_materiality
# =============================================================================

class TestMateriality:
    def test_high_threshold(self):
        assert _classify_materiality(Decimal("1000000")) == "high"
        assert _classify_materiality(Decimal("5000000")) == "high"

    def test_medium_threshold(self):
        assert _classify_materiality(Decimal("100000")) == "medium"
        assert _classify_materiality(Decimal("500000")) == "medium"

    def test_low_threshold(self):
        assert _classify_materiality(Decimal("10000")) == "low"
        assert _classify_materiality(Decimal("50000")) == "low"

    def test_info_below_low(self):
        assert _classify_materiality(Decimal("0")) == "info"
        assert _classify_materiality(Decimal("9999")) == "info"

    def test_negative_values_use_absolute(self):
        assert _classify_materiality(Decimal("-2000000")) == "high"
        assert _classify_materiality(Decimal("-50000")) == "low"


# =============================================================================
# Tests — PnLAgentInput
# =============================================================================

class TestPnLAgentInput:
    def test_accepts_valid(self, utc_now):
        inp = PnLAgentInput(
            starting_capital_krw=Decimal("100000000"),
            ending_capital_krw=Decimal("99500000"),
            operator_query="손실 분석",
            requested_at_utc=utc_now,
        )
        assert inp.session_pnl_krw == Decimal("-500000")

    def test_rejects_non_decimal(self, utc_now):
        with pytest.raises(TypeError, match="must be Decimal"):
            PnLAgentInput(
                starting_capital_krw=100,  # int
                ending_capital_krw=Decimal("100"),
                operator_query="x",
                requested_at_utc=utc_now,
            )

    def test_rejects_zero_starting(self, utc_now):
        with pytest.raises(ValueError, match="must be positive"):
            PnLAgentInput(
                starting_capital_krw=Decimal("0"),
                ending_capital_krw=Decimal("0"),
                operator_query="x",
                requested_at_utc=utc_now,
            )

    def test_rejects_negative_ending(self, utc_now):
        with pytest.raises(ValueError, match="non-negative"):
            PnLAgentInput(
                starting_capital_krw=Decimal("1"),
                ending_capital_krw=Decimal("-1"),
                operator_query="x",
                requested_at_utc=utc_now,
            )

    def test_rejects_naive_datetime(self):
        with pytest.raises(ValueError, match="tz-aware"):
            PnLAgentInput(
                starting_capital_krw=Decimal("1"),
                ending_capital_krw=Decimal("1"),
                operator_query="x",
                requested_at_utc=datetime(2026, 5, 7),
            )

    def test_rejects_bad_window(self, utc_now):
        with pytest.raises(ValueError, match="session_window_hours"):
            PnLAgentInput(
                starting_capital_krw=Decimal("1"),
                ending_capital_krw=Decimal("1"),
                operator_query="x",
                requested_at_utc=utc_now,
                session_window_hours=0,
            )
        with pytest.raises(ValueError, match="session_window_hours"):
            PnLAgentInput(
                starting_capital_krw=Decimal("1"),
                ending_capital_krw=Decimal("1"),
                operator_query="x",
                requested_at_utc=utc_now,
                session_window_hours=200,
            )


# =============================================================================
# Tests — PnLDecomposition / AttributionRow
# =============================================================================

class TestDataclasses:
    def test_decomposition_requires_decimal(self):
        with pytest.raises(TypeError, match="must be Decimal"):
            PnLDecomposition(
                realized_krw=100,  # int
                unrealized_krw=Decimal("0"),
                fees_krw=Decimal("0"),
                slippage_krw=Decimal("0"),
                gross_pnl_krw=Decimal("0"),
                net_pnl_krw=Decimal("0"),
            )

    def test_attribution_row_rejects_empty_key(self):
        with pytest.raises(ValueError, match="non-empty"):
            AttributionRow(
                key="",
                realized_krw=Decimal("0"),
                unrealized_krw=Decimal("0"),
                net_pnl_krw=Decimal("0"),
                trade_count=0,
                materiality="info",
            )

    def test_attribution_row_rejects_negative_count(self):
        with pytest.raises(ValueError, match="non-negative"):
            AttributionRow(
                key="x",
                realized_krw=Decimal("0"),
                unrealized_krw=Decimal("0"),
                net_pnl_krw=Decimal("0"),
                trade_count=-1,
                materiality="info",
            )


# =============================================================================
# Tests — ALLOWED_TOOLS allowlist
# =============================================================================

class TestAllowedTools:
    def test_only_read_only_tools(self):
        for tool in ALLOWED_TOOLS:
            assert tool.startswith("get_"), f"{tool} is not read-only"

    def test_no_write_tools_in_allowlist(self):
        forbidden = {"approve_action", "reject_action",
                     "execute_approved_action", "request_cancel_order"}
        assert ALLOWED_TOOLS.isdisjoint(forbidden)


# =============================================================================
# Tests — Tool collector
# =============================================================================

class TestToolCollector:
    def test_collects_five_tools_in_order(self, mcp_normal_pnl):
        results = _collect_pnl_tools(mcp_normal_pnl)
        assert len(results) == 5
        expected = [
            "get_market_status", "get_pnl_snapshot", "get_recent_fills",
            "get_positions", "get_strategy_registry",
        ]
        assert [r.tool_name for r in results] == expected

    def test_passes_window_hours_to_snapshot(self, mcp_normal_pnl):
        _collect_pnl_tools(mcp_normal_pnl, session_window_hours=48)
        # Find the get_pnl_snapshot call
        snapshot_call = next(c for c in mcp_normal_pnl.calls
                             if c[0] == "get_pnl_snapshot")
        assert snapshot_call[1] == {"window_hours": 48}

    def test_failed_tool_does_not_abort(self):
        mcp = _StubMCPReadOnlyClient(
            responses={t: {} for t in ALLOWED_TOOLS},
            fail_tools={"get_recent_fills"},
        )
        results = _collect_pnl_tools(mcp)
        assert len(results) == 5
        fills = next(r for r in results if r.tool_name == "get_recent_fills")
        assert fills.success is False
        assert "error" in fills.data


# =============================================================================
# Tests — Decomposition extraction
# =============================================================================

class TestDecomposition:
    def test_uses_snapshot_values(self, mcp_normal_pnl):
        results = _collect_pnl_tools(mcp_normal_pnl)
        decomp = _extract_decomposition(results)
        assert decomp.realized_krw == Decimal("150000")
        assert decomp.unrealized_krw == Decimal("50000")
        assert decomp.fees_krw == Decimal("5000")
        assert decomp.slippage_krw == Decimal("8000")
        assert decomp.gross_pnl_krw == Decimal("200000")
        assert decomp.net_pnl_krw == Decimal("187000")  # 200k - 5k - 8k

    def test_computes_slippage_from_fills_when_snapshot_missing(self):
        mcp = _StubMCPReadOnlyClient(responses={
            "get_market_status": {},
            "get_pnl_snapshot": {
                "realized_krw": "0", "unrealized_krw": "0", "fees_krw": "0",
                # slippage_krw intentionally absent → fallback to fills
            },
            "get_recent_fills": {"fills": [
                {"side": "buy", "arrival_price_krw": "100",
                 "fill_price_krw": "102", "qty": "10"},  # +20 slippage cost
                {"side": "sell", "arrival_price_krw": "100",
                 "fill_price_krw": "98", "qty": "5"},   # +10 slippage cost
            ]},
            "get_positions": {},
            "get_strategy_registry": {},
        })
        results = _collect_pnl_tools(mcp)
        decomp = _extract_decomposition(results)
        assert decomp.slippage_krw == Decimal("30")  # 20 + 10

    def test_handles_missing_data_gracefully(self):
        mcp = _StubMCPReadOnlyClient(responses={t: {} for t in ALLOWED_TOOLS})
        results = _collect_pnl_tools(mcp)
        decomp = _extract_decomposition(results)
        assert decomp.realized_krw == Decimal("0")
        assert decomp.net_pnl_krw == Decimal("0")


# =============================================================================
# Tests — Attribution
# =============================================================================

class TestAttribution:
    def test_aggregates_strategy_and_symbol(self, mcp_normal_pnl):
        results = _collect_pnl_tools(mcp_normal_pnl)
        strat, sym = _build_attribution(results)
        # All fills attributed to momentum_v1
        assert len(strat) >= 1
        assert strat[0].key == "momentum_v1"
        assert strat[0].realized_krw == Decimal("150000")
        assert strat[0].unrealized_krw == Decimal("50000")
        assert strat[0].trade_count == 2
        # Symbols: 005930, 000660, 035420 (035420 only in positions)
        symbol_keys = {r.key for r in sym}
        assert {"005930", "000660", "035420"}.issubset(symbol_keys)

    def test_sorted_by_absolute_pnl_desc(self, mcp_loss_pnl):
        results = _collect_pnl_tools(mcp_loss_pnl)
        strat, sym = _build_attribution(results)
        # Top symbol should be 005930 (-1.5M, biggest absolute)
        assert sym[0].key == "005930"
        assert sym[0].realized_krw == Decimal("-1500000")

    def test_caps_at_max_rows(self):
        fills = [
            {"strategy_id": f"s{i}", "symbol": f"X{i:03d}",
             "realized_pnl_krw": str(i * 1000), "side": "buy",
             "arrival_price_krw": "100", "fill_price_krw": "100", "qty": "1"}
            for i in range(30)
        ]
        mcp = _StubMCPReadOnlyClient(responses={
            "get_market_status": {},
            "get_pnl_snapshot": {},
            "get_recent_fills": {"fills": fills},
            "get_positions": {},
            "get_strategy_registry": {},
        })
        results = _collect_pnl_tools(mcp)
        strat, sym = _build_attribution(results)
        assert len(strat) <= 10
        assert len(sym) <= 15

    def test_empty_data_returns_empty_tuples(self):
        mcp = _StubMCPReadOnlyClient(responses={t: {} for t in ALLOWED_TOOLS})
        results = _collect_pnl_tools(mcp)
        strat, sym = _build_attribution(results)
        assert strat == ()
        assert sym == ()


# =============================================================================
# Tests — Notable events
# =============================================================================

class TestNotableEvents:
    def test_extracts_with_order_id_masked(self, mcp_loss_pnl):
        results = _collect_pnl_tools(mcp_loss_pnl)
        events = _extract_notable_events(results)
        assert len(events) == 1
        evt = events[0]
        assert evt["event_type"] == "stop_loss_hit"
        # Critical: order_id_masked must NOT contain full ID
        assert "1234567890XYZ" not in evt["order_id_masked"]
        assert evt["order_id_masked"].endswith("...")

    def test_no_events_returns_empty(self, mcp_normal_pnl):
        results = _collect_pnl_tools(mcp_normal_pnl)
        events = _extract_notable_events(results)
        assert events == ()


# =============================================================================
# Tests — Fallback builder
# =============================================================================

class TestFallback:
    def test_normal_payload_has_all_keys(self, mcp_normal_pnl):
        results = _collect_pnl_tools(mcp_normal_pnl)
        payload = _build_pnl_fallback(results, "test query")
        required = {
            "summary_kr", "materiality_overall", "decomposition",
            "strategy_attribution", "symbol_attribution",
            "notable_events", "operator_query_echo", "fallback_used",
        }
        assert required.issubset(payload.keys())
        assert payload["fallback_used"] is True

    def test_loss_payload_classifies_high(self, mcp_loss_pnl):
        results = _collect_pnl_tools(mcp_loss_pnl)
        payload = _build_pnl_fallback(results, "loss analysis")
        assert payload["materiality_overall"] == "high"
        assert "손실" in payload["summary_kr"]
        assert Decimal(payload["decomposition"]["net_pnl_krw"]) < Decimal("0")

    def test_profit_payload_classifies_correctly(self, mcp_normal_pnl):
        results = _collect_pnl_tools(mcp_normal_pnl)
        payload = _build_pnl_fallback(results, "profit check")
        assert "수익" in payload["summary_kr"]
        assert Decimal(payload["decomposition"]["net_pnl_krw"]) > Decimal("0")

    def test_query_echo_truncated(self, mcp_normal_pnl):
        results = _collect_pnl_tools(mcp_normal_pnl)
        long_query = "x" * 1000
        payload = _build_pnl_fallback(results, long_query)
        assert len(payload["operator_query_echo"]) <= 500


# =============================================================================
# Tests — End-to-end agent
# =============================================================================

class TestPnLExplanationAgent:
    def test_agent_runs_normal(self, llm_stub, mcp_normal_pnl):
        agent = PnLExplanationAgent(llm_stub, mcp_normal_pnl)
        report = agent.explain_pnl(
            starting_capital_krw=Decimal("100000000"),
            ending_capital_krw=Decimal("100200000"),
            operator_query="오늘 수익 분석",
        )
        assert isinstance(report, PnLAgentReport)
        assert report.decomposition.realized_krw == Decimal("150000")
        assert len(report.strategy_attribution) >= 1
        assert report.fallback_used is True

    def test_agent_runs_loss(self, llm_stub, mcp_loss_pnl):
        agent = PnLExplanationAgent(llm_stub, mcp_loss_pnl)
        report = agent.explain_pnl(
            starting_capital_krw=Decimal("100000000"),
            ending_capital_krw=Decimal("97500000"),
            operator_query="손실 사유 분석",
        )
        assert report.materiality_overall == "high"
        assert report.decomposition.realized_krw < Decimal("0")
        assert len(report.notable_events) >= 1
        # PII check — full order ID must not appear in any notable_event
        for evt in report.notable_events:
            assert "1234567890XYZ" not in evt.get("order_id_masked", "")

    def test_agent_session_window_propagates(self, llm_stub, mcp_normal_pnl):
        agent = PnLExplanationAgent(llm_stub, mcp_normal_pnl)
        agent.explain_pnl(
            starting_capital_krw=Decimal("1"),
            ending_capital_krw=Decimal("1"),
            operator_query="x",
            session_window_hours=72,
        )
        snapshot_calls = [c for c in mcp_normal_pnl.calls
                          if c[0] == "get_pnl_snapshot"]
        assert snapshot_calls[-1][1] == {"window_hours": 72}

    def test_agent_rejects_none_clients(self, mcp_normal_pnl, llm_stub):
        with pytest.raises(ValueError, match="llm_client"):
            PnLExplanationAgent(None, mcp_normal_pnl)
        with pytest.raises(ValueError, match="mcp_client"):
            PnLExplanationAgent(llm_stub, None)

    def test_agent_rejects_invalid_max_tool_calls(self, llm_stub, mcp_normal_pnl):
        with pytest.raises(ValueError, match="max_tool_calls"):
            PnLExplanationAgent(llm_stub, mcp_normal_pnl, max_tool_calls=0)

    def test_audit_failure_does_not_crash(self, llm_stub, mcp_normal_pnl):
        broken = MagicMock()
        broken.write_event.side_effect = RuntimeError("audit dead")
        agent = PnLExplanationAgent(llm_stub, mcp_normal_pnl, audit_writer=broken)
        report = agent.explain_pnl(
            starting_capital_krw=Decimal("1"),
            ending_capital_krw=Decimal("1"),
            operator_query="x",
        )
        assert isinstance(report, PnLAgentReport)

    def test_agent_drops_invalid_attribution_rows(self, llm_stub, mcp_normal_pnl):
        """Defense-in-depth: bad rows from LLM are silently dropped."""
        agent = PnLExplanationAgent(llm_stub, mcp_normal_pnl)
        agent._runner.next_payload = {
            "summary_kr": "test",
            "materiality_overall": "low",
            "decomposition": {
                "realized_krw": "100", "unrealized_krw": "0",
                "fees_krw": "0", "slippage_krw": "0",
                "gross_pnl_krw": "100", "net_pnl_krw": "100",
            },
            "strategy_attribution": [
                {"key": "good", "realized_krw": "100", "unrealized_krw": "0",
                 "net_pnl_krw": "100", "trade_count": 1, "materiality": "low"},
                {"key": "", "realized_krw": "50", "unrealized_krw": "0",  # bad: empty key
                 "net_pnl_krw": "50", "trade_count": 1, "materiality": "low"},
            ],
            "symbol_attribution": [],
            "notable_events": [],
        }
        agent._runner.next_fallback_used = False
        report = agent.explain_pnl(
            starting_capital_krw=Decimal("1"),
            ending_capital_krw=Decimal("1"),
            operator_query="x",
        )
        # Only the good row should survive
        assert len(report.strategy_attribution) == 1
        assert report.strategy_attribution[0].key == "good"

    def test_to_audit_dict_excludes_pii(self, llm_stub, mcp_loss_pnl):
        agent = PnLExplanationAgent(llm_stub, mcp_loss_pnl)
        report = agent.explain_pnl(
            starting_capital_krw=Decimal("100000000"),
            ending_capital_krw=Decimal("97500000"),
            operator_query="x",
        )
        audit = report.to_audit_dict()
        # Full audit dict must not contain raw order IDs
        s = str(audit)
        assert "1234567890XYZ" not in s
        assert "trace_id" in audit
        assert "net_pnl_krw" in audit


# =============================================================================
# Tests — Numerical correctness (Decimal precision)
# =============================================================================

class TestDecimalPrecision:
    def test_no_float_used_in_decomposition(self, mcp_normal_pnl):
        results = _collect_pnl_tools(mcp_normal_pnl)
        decomp = _extract_decomposition(results)
        for name in ("realized_krw", "unrealized_krw", "fees_krw",
                     "slippage_krw", "gross_pnl_krw", "net_pnl_krw"):
            value = getattr(decomp, name)
            assert isinstance(value, Decimal)
            assert not isinstance(value, float)

    def test_session_pnl_arithmetic_exact(self, utc_now):
        # 0.1 + 0.2 == 0.3 in Decimal but not float
        inp = PnLAgentInput(
            starting_capital_krw=Decimal("1.1"),
            ending_capital_krw=Decimal("1.4"),
            operator_query="x",
            requested_at_utc=utc_now,
        )
        # Decimal('1.4') - Decimal('1.1') = Decimal('0.3') exactly
        assert inp.session_pnl_krw == Decimal("0.3")
