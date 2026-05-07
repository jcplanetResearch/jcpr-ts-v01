"""Task 38 — Risk Explanation Agent.

Combines Task 35 (restricted MCP audit) + Task 47 (portfolio_risk) data and
explains risk-limit violations to the operator in Korean. Suggests Task 35
write-tool candidates as advisory only — never invokes them itself.

Security guarantees (defense in depth):
    1. No write-tool invocation. The agent only PROPOSES request_* candidates
       in its response payload; the operator must invoke Task 35 restricted
       MCP separately under the 3-phase approval workflow.
    2. Read-only data sources only: get_portfolio_risk (Task 47),
       get_rejection_summary (Task 35 audit), get_positions, get_pnl_snapshot.
    3. No external network calls. Uses MockLLMClient or any LLMClient impl.
    4. All decisions logged via TraceContext + AuditWriter.
    5. Decimal-only arithmetic; frozen dataclasses; UTC tz-aware timestamps.

Dependencies (must exist in the operator's local repo):
    - src/agents/_llm_client.py     (Task 37)
    - src/agents/_mcp_client.py     (Task 37) — MCPReadOnlyClient
    - src/agents/_agent_runner.py   (Task 37) — AgentRunner, AgentSpec
    - src/agents/prompts/           (Task 36) — risk_explanation schema
    - src/observability/            (Tasks A1-A3)

Interface contract:
    Input:  starting_capital_krw: Decimal, current_cash_krw: Decimal,
            operator_query: str, parent_trace: TraceContext | None
    Output: RiskAgentReport (frozen) with schema-validated payload
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

# ---- Local package imports (resolved at runtime in operator's repo) ---------
# These imports rely on Task 36 (prompts) + Task 37 (runner, llm, mcp client).
# We import lazily inside functions where possible to keep this module robust
# against partial-sync scenarios during development.
from ._agent_runner import AgentRunner, AgentSpec, AgentRunResult
from ._llm_client import LLMClient
from ._mcp_client import MCPReadOnlyClient, MCPCallResult


# =============================================================================
# Constants — risk severity thresholds and tool routing
# =============================================================================

#: Severity level vocabulary. Order matters: index used for max() aggregation.
SEVERITY_LEVELS: tuple[str, ...] = ("info", "low", "medium", "high", "critical")

#: Maximum number of action candidates returned in the response payload.
MAX_ACTION_CANDIDATES: int = 5

#: Maximum number of rejected-order entries summarized in the prompt.
MAX_REJECTION_SAMPLE: int = 10

#: Read-only MCP tools the risk agent may call. Hard-coded allowlist;
#: any attempt to invoke a tool outside this set raises RiskAgentError.
ALLOWED_TOOLS: frozenset[str] = frozenset({
    "get_portfolio_risk",
    "get_rejection_summary",
    "get_positions",
    "get_pnl_snapshot",
    "get_market_status",
})

#: Write-tool candidate vocabulary the agent may SUGGEST (never call).
#: Mirrors Task 35 restricted MCP request_* surface.
SUGGESTABLE_WRITE_TOOLS: frozenset[str] = frozenset({
    "request_cancel_order",
    "request_flatten_position",
    "request_strategy_pause",
    "request_capacity_reduce",
    "request_kill_switch_engage",
})

#: Maps risk-limit violation types to suggested write tools (in priority order).
ACTION_ROUTING: Mapping[str, tuple[str, ...]] = {
    "position_limit_breach": ("request_flatten_position", "request_cancel_order"),
    "exposure_limit_breach": ("request_flatten_position", "request_strategy_pause"),
    "drawdown_breach": ("request_kill_switch_engage", "request_strategy_pause"),
    "concentration_breach": ("request_flatten_position", "request_capacity_reduce"),
    "rejection_spike": ("request_strategy_pause", "request_capacity_reduce"),
    "var_breach": ("request_capacity_reduce", "request_strategy_pause"),
}


class RiskAgentError(RuntimeError):
    """Raised when risk agent invariants are violated."""


# =============================================================================
# Frozen data structures — immutable input/output contracts
# =============================================================================

@dataclass(frozen=True, slots=True)
class RiskAgentInput:
    """Operator-supplied context for a risk-explanation run."""
    starting_capital_krw: Decimal
    current_cash_krw: Decimal
    operator_query: str
    requested_at_utc: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.starting_capital_krw, Decimal):
            raise TypeError("starting_capital_krw must be Decimal")
        if not isinstance(self.current_cash_krw, Decimal):
            raise TypeError("current_cash_krw must be Decimal")
        if self.starting_capital_krw <= Decimal("0"):
            raise ValueError("starting_capital_krw must be positive")
        if self.current_cash_krw < Decimal("0"):
            raise ValueError("current_cash_krw must be non-negative")
        if not isinstance(self.operator_query, str) or not self.operator_query.strip():
            raise ValueError("operator_query must be non-empty string")
        if self.requested_at_utc.tzinfo is None:
            raise ValueError("requested_at_utc must be tz-aware (UTC)")
        if self.requested_at_utc.tzinfo.utcoffset(self.requested_at_utc) != timezone.utc.utcoffset(self.requested_at_utc):
            # Allow any tz-aware datetime but normalize via tzinfo presence
            pass


@dataclass(frozen=True, slots=True)
class ActionCandidate:
    """A suggested write-tool invocation. Advisory only — never executed."""
    tool_name: str
    rationale_kr: str
    parameters_preview: Mapping[str, Any]
    severity: str
    requires_human_approval: bool = True
    executed: bool = False

    def __post_init__(self) -> None:
        if self.tool_name not in SUGGESTABLE_WRITE_TOOLS:
            raise RiskAgentError(
                f"tool_name '{self.tool_name}' not in suggestable allowlist"
            )
        if self.severity not in SEVERITY_LEVELS:
            raise ValueError(f"severity must be one of {SEVERITY_LEVELS}")
        if self.executed:
            raise RiskAgentError(
                "ActionCandidate.executed must be False — agent never executes"
            )
        if not self.requires_human_approval:
            raise RiskAgentError(
                "ActionCandidate.requires_human_approval must be True"
            )


@dataclass(frozen=True, slots=True)
class RiskAgentReport:
    """Final risk-explanation report returned to operator."""
    trace_id: str
    summary_kr: str
    severity_overall: str
    breach_count: int
    rejection_count_24h: int
    action_candidates: tuple[ActionCandidate, ...]
    raw_llm_response: str
    fallback_used: bool
    schema_validated: bool
    elapsed_ms: int
    generated_at_utc: datetime

    def to_audit_dict(self) -> dict[str, Any]:
        """Return a dict suitable for AuditWriter (no PII, no secrets)."""
        return {
            "trace_id": self.trace_id,
            "severity_overall": self.severity_overall,
            "breach_count": self.breach_count,
            "rejection_count_24h": self.rejection_count_24h,
            "action_candidate_count": len(self.action_candidates),
            "fallback_used": self.fallback_used,
            "schema_validated": self.schema_validated,
            "elapsed_ms": self.elapsed_ms,
            "generated_at_utc": self.generated_at_utc.isoformat(),
        }


# =============================================================================
# Tool collector — combines audit + portfolio_risk data sources
# =============================================================================

def _collect_risk_tools(
    mcp_client: MCPReadOnlyClient,
    *,
    parent_trace: Any | None = None,
) -> tuple[MCPCallResult, ...]:
    """Gather risk-relevant data from both Task 47 portfolio_risk and Task 35 audit.

    Order of calls (sequential, deterministic):
        1. get_market_status     — sanity check (market open/closed)
        2. get_portfolio_risk    — Task 47: current exposure, VaR, concentration
        3. get_rejection_summary — Task 35 audit: recent rejected orders
        4. get_positions         — current holdings (sized context)
        5. get_pnl_snapshot      — drawdown context

    All calls go through MCPReadOnlyClient.call() which is hard-coded to
    read-only mapping. Any error in one call does not abort the chain;
    failed results carry success=False and downstream tools still run.
    """
    results: list[MCPCallResult] = []
    tools_to_call: tuple[tuple[str, dict[str, Any]], ...] = (
        ("get_market_status", {}),
        ("get_portfolio_risk", {}),
        ("get_rejection_summary", {"hours": 24}),
        ("get_positions", {}),
        ("get_pnl_snapshot", {}),
    )
    for tool_name, kwargs in tools_to_call:
        if tool_name not in ALLOWED_TOOLS:
            # Defense in depth — should be unreachable
            raise RiskAgentError(f"tool_name '{tool_name}' violates ALLOWED_TOOLS")
        try:
            result = mcp_client.call(tool_name, **kwargs)
        except Exception as exc:  # noqa: BLE001 — agent must not crash on tool failure
            result = MCPCallResult(
                tool_name=tool_name,
                success=False,
                data={"error": type(exc).__name__, "message": str(exc)[:200]},
                trace_id=getattr(parent_trace, "trace_id", "n/a") if parent_trace else "n/a",
                elapsed_ms=0,
            )
        results.append(result)
    return tuple(results)


# =============================================================================
# Severity aggregation
# =============================================================================

def _max_severity(severities: Iterable[str]) -> str:
    """Return the highest-priority severity from an iterable. Defaults to 'info'."""
    max_idx = 0
    for s in severities:
        if s in SEVERITY_LEVELS:
            idx = SEVERITY_LEVELS.index(s)
            if idx > max_idx:
                max_idx = idx
    return SEVERITY_LEVELS[max_idx]


def _classify_breach(breach_type: str, ratio: Decimal) -> str:
    """Map (breach_type, breach_ratio) -> severity level.

    Ratio is breach_value / limit_value. >= 1.0 means at-limit; >= 1.5 critical.
    """
    if ratio >= Decimal("2.0"):
        return "critical"
    if ratio >= Decimal("1.5"):
        return "high"
    if ratio >= Decimal("1.2"):
        return "medium"
    if ratio >= Decimal("1.0"):
        return "low"
    return "info"


# =============================================================================
# Action candidate builder
# =============================================================================

def _build_action_candidates(
    breaches: Sequence[Mapping[str, Any]],
    *,
    rejection_count: int,
) -> tuple[ActionCandidate, ...]:
    """Translate detected breaches into a deduplicated tuple of ActionCandidate.

    Returns at most MAX_ACTION_CANDIDATES, sorted by severity (critical first).
    """
    candidates: list[ActionCandidate] = []
    seen_tools: set[str] = set()

    # Sort breaches by severity desc so highest-priority routes win on dedup.
    sorted_breaches = sorted(
        breaches,
        key=lambda b: SEVERITY_LEVELS.index(b.get("severity", "info")),
        reverse=True,
    )

    for breach in sorted_breaches:
        breach_type = breach.get("type", "")
        severity = breach.get("severity", "info")
        symbol = breach.get("symbol")
        order_id = breach.get("order_id")
        strategy_id = breach.get("strategy_id")

        tool_chain = ACTION_ROUTING.get(breach_type, ())
        for tool_name in tool_chain:
            if tool_name in seen_tools:
                continue
            if len(candidates) >= MAX_ACTION_CANDIDATES:
                break
            params: dict[str, Any] = {}
            if tool_name == "request_cancel_order" and order_id:
                params["order_id"] = str(order_id)
            elif tool_name == "request_flatten_position" and symbol:
                params["symbol"] = str(symbol)
            elif tool_name == "request_strategy_pause" and strategy_id:
                params["strategy_id"] = str(strategy_id)
            elif tool_name == "request_capacity_reduce":
                params["reduction_pct"] = "50"
            elif tool_name == "request_kill_switch_engage":
                params["reason_kr"] = breach.get("description_kr", "위험 한도 위반")

            rationale = (
                f"{breach.get('description_kr', breach_type)} "
                f"(severity={severity})"
            )
            try:
                candidate = ActionCandidate(
                    tool_name=tool_name,
                    rationale_kr=rationale,
                    parameters_preview=params,
                    severity=severity,
                )
            except RiskAgentError:
                continue
            candidates.append(candidate)
            seen_tools.add(tool_name)

    # Rejection spike heuristic — if rejections > 5 in 24h and no breach matched
    if rejection_count > 5 and not any(
        c.tool_name == "request_strategy_pause" for c in candidates
    ) and len(candidates) < MAX_ACTION_CANDIDATES:
        sev = "high" if rejection_count > 20 else "medium"
        candidates.append(ActionCandidate(
            tool_name="request_strategy_pause",
            rationale_kr=(
                f"24시간 거부 주문(rejected orders) {rejection_count}건 — "
                f"전략 일시중지 권고"
            ),
            parameters_preview={"strategy_id": "all", "reason": "rejection_spike"},
            severity=sev,
        ))

    return tuple(candidates[:MAX_ACTION_CANDIDATES])


# =============================================================================
# Fallback builder — schema-conforming response when LLM fails
# =============================================================================

def _extract_breaches_from_tools(
    tool_results: Sequence[MCPCallResult],
) -> tuple[list[Mapping[str, Any]], int]:
    """Walk tool results and extract structured breach list + rejection count."""
    breaches: list[Mapping[str, Any]] = []
    rejection_count = 0

    for result in tool_results:
        if not result.success:
            continue
        data = result.data or {}

        if result.tool_name == "get_portfolio_risk":
            # Task 47 returns: {"breaches": [{type, severity, ratio, symbol, ...}], ...}
            for b in data.get("breaches", []):
                if not isinstance(b, Mapping):
                    continue
                breach_type = b.get("type", "unknown")
                ratio_raw = b.get("ratio", "1.0")
                try:
                    ratio = Decimal(str(ratio_raw))
                except Exception:
                    ratio = Decimal("1.0")
                severity = b.get("severity") or _classify_breach(breach_type, ratio)
                breaches.append({
                    "type": breach_type,
                    "severity": severity,
                    "ratio": str(ratio),
                    "symbol": b.get("symbol"),
                    "strategy_id": b.get("strategy_id"),
                    "description_kr": b.get("description_kr") or _default_description_kr(breach_type),
                })

        elif result.tool_name == "get_rejection_summary":
            count = data.get("count_24h", data.get("count", 0))
            if isinstance(count, int):
                rejection_count = count
            elif isinstance(count, str) and count.isdigit():
                rejection_count = int(count)

            # Some rejections may indicate breach patterns
            for r in data.get("recent_rejections", [])[:MAX_REJECTION_SAMPLE]:
                if not isinstance(r, Mapping):
                    continue
                reason = r.get("reason_code", "")
                if reason in ("position_limit", "exposure_limit"):
                    breaches.append({
                        "type": f"{reason}_breach",
                        "severity": "medium",
                        "ratio": "1.0",
                        "symbol": r.get("symbol"),
                        "order_id": r.get("client_order_id"),
                        "strategy_id": r.get("strategy_id"),
                        "description_kr": (
                            f"주문 거부(order rejected): {reason} — {r.get('symbol', 'N/A')}"
                        ),
                    })

    return breaches, rejection_count


def _default_description_kr(breach_type: str) -> str:
    """Fallback Korean description for a breach type."""
    descriptions = {
        "position_limit_breach": "포지션 한도(position limit) 초과",
        "exposure_limit_breach": "익스포저 한도(exposure limit) 초과",
        "drawdown_breach": "손실 한도(drawdown limit) 도달",
        "concentration_breach": "집중도(concentration) 한도 초과",
        "var_breach": "VaR(Value-at-Risk) 한도 초과",
        "rejection_spike": "주문 거부(rejection) 급증",
    }
    return descriptions.get(breach_type, f"위험 한도 위반(risk breach): {breach_type}")


def _build_risk_fallback(
    tool_results: Sequence[MCPCallResult],
    operator_query: str,
) -> dict[str, Any]:
    """Build a schema-valid risk_explanation payload directly from tool results.

    Used when LLM fails or schema validation rejects the LLM output. The payload
    must satisfy prompts/schemas/risk_explanation.json (existing schema).
    """
    breaches, rejection_count = _extract_breaches_from_tools(tool_results)

    severities = [b.get("severity", "info") for b in breaches]
    if rejection_count > 5:
        severities.append("medium" if rejection_count <= 20 else "high")
    overall = _max_severity(severities) if severities else "info"

    candidates = _build_action_candidates(breaches, rejection_count=rejection_count)

    if breaches:
        first = breaches[0]
        summary_kr = (
            f"감지된 위험 한도 위반(detected breaches) {len(breaches)}건. "
            f"가장 심각한 항목: {first.get('description_kr', '알 수 없음')} "
            f"(severity={first.get('severity', 'info')}). "
            f"24시간 거부 주문 {rejection_count}건."
        )
    elif rejection_count > 5:
        summary_kr = (
            f"한도 위반은 없으나 24시간 거부 주문(rejected orders)이 "
            f"{rejection_count}건으로 평소보다 많습니다. 전략 점검 권고."
        )
    else:
        summary_kr = (
            f"현재 위험 한도(risk limits) 내 정상 운영 중. "
            f"감지된 위반 없음, 24시간 거부 주문 {rejection_count}건."
        )

    return {
        "summary_kr": summary_kr,
        "severity_overall": overall,
        "breach_count": len(breaches),
        "rejection_count_24h": rejection_count,
        "breaches": [dict(b) for b in breaches[:MAX_REJECTION_SAMPLE]],
        "action_candidates": [
            {
                "tool_name": c.tool_name,
                "rationale_kr": c.rationale_kr,
                "parameters_preview": dict(c.parameters_preview),
                "severity": c.severity,
                "requires_human_approval": c.requires_human_approval,
                "executed": c.executed,
            }
            for c in candidates
        ],
        "operator_query_echo": operator_query[:500],
        "fallback_used": True,
    }


# =============================================================================
# Main agent class
# =============================================================================

class RiskExplanationAgent:
    """Operator-facing agent that explains risk-limit violations in Korean.

    Reuses Task 37 AgentRunner with a Task 38-specific AgentSpec.

    Usage:
        agent = RiskExplanationAgent(llm_client, mcp_client, audit_writer)
        report = agent.explain_risk(
            starting_capital_krw=Decimal("100000000"),
            current_cash_krw=Decimal("80000000"),
            operator_query="현재 위험 상태 요약",
        )
        # report.action_candidates contains advisory-only suggestions
        # operator must invoke Task 35 restricted MCP separately
    """

    SYSTEM_TEMPLATE_ID: str = "risk_analyst_system"
    USER_TEMPLATE_ID: str = "risk_explanation_task"
    SCHEMA_ID: str = "risk_explanation"
    AGENT_NAME: str = "risk_explanation_agent"

    def __init__(
        self,
        llm_client: LLMClient,
        mcp_client: MCPReadOnlyClient,
        *,
        audit_writer: Any | None = None,
        max_tool_calls: int = 5,
    ) -> None:
        if llm_client is None:
            raise ValueError("llm_client must not be None")
        if mcp_client is None:
            raise ValueError("mcp_client must not be None")
        if max_tool_calls < 1 or max_tool_calls > 10:
            raise ValueError("max_tool_calls must be 1..10")

        self._llm = llm_client
        self._mcp = mcp_client
        self._audit = audit_writer
        self._max_tool_calls = max_tool_calls

        self._spec = AgentSpec(
            agent_name=self.AGENT_NAME,
            system_template_id=self.SYSTEM_TEMPLATE_ID,
            user_template_id=self.USER_TEMPLATE_ID,
            schema_id=self.SCHEMA_ID,
            tool_collector=lambda parent_trace=None: _collect_risk_tools(
                self._mcp, parent_trace=parent_trace,
            ),
            fallback_builder=_build_risk_fallback,
            max_tool_calls=max_tool_calls,
        )
        self._runner = AgentRunner(
            llm_client=self._llm,
            mcp_client=self._mcp,
            audit_writer=self._audit,
            spec=self._spec,
        )

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    def explain_risk(
        self,
        *,
        starting_capital_krw: Decimal,
        current_cash_krw: Decimal,
        operator_query: str,
        parent_trace: Any | None = None,
    ) -> RiskAgentReport:
        """Run the full risk-explanation pipeline."""
        agent_input = RiskAgentInput(
            starting_capital_krw=starting_capital_krw,
            current_cash_krw=current_cash_krw,
            operator_query=operator_query,
            requested_at_utc=datetime.now(tz=timezone.utc),
        )

        run_result: AgentRunResult = self._runner.run(
            template_variables={
                "starting_capital_krw": str(agent_input.starting_capital_krw),
                "current_cash_krw": str(agent_input.current_cash_krw),
                "operator_query": agent_input.operator_query,
                "requested_at_utc": agent_input.requested_at_utc.isoformat(),
            },
            parent_trace=parent_trace,
        )

        payload = run_result.payload
        candidates = self._extract_candidates(payload)
        report = RiskAgentReport(
            trace_id=run_result.trace_id,
            summary_kr=str(payload.get("summary_kr", "(요약 없음)")),
            severity_overall=str(payload.get("severity_overall", "info")),
            breach_count=int(payload.get("breach_count", 0)),
            rejection_count_24h=int(payload.get("rejection_count_24h", 0)),
            action_candidates=candidates,
            raw_llm_response=run_result.raw_llm_response,
            fallback_used=run_result.fallback_used,
            schema_validated=run_result.schema_validated,
            elapsed_ms=run_result.elapsed_ms,
            generated_at_utc=datetime.now(tz=timezone.utc),
        )

        self._audit_emit(report)
        return report

    # -------------------------------------------------------------------------
    # Internals
    # -------------------------------------------------------------------------

    def _extract_candidates(
        self, payload: Mapping[str, Any]
    ) -> tuple[ActionCandidate, ...]:
        """Convert payload action_candidates list to ActionCandidate tuple.

        Filters out any candidate referencing a tool outside SUGGESTABLE_WRITE_TOOLS.
        Defense in depth — even if LLM hallucinated a tool name.
        """
        raw = payload.get("action_candidates", [])
        if not isinstance(raw, list):
            return ()
        out: list[ActionCandidate] = []
        for item in raw[:MAX_ACTION_CANDIDATES]:
            if not isinstance(item, Mapping):
                continue
            tool_name = item.get("tool_name", "")
            if tool_name not in SUGGESTABLE_WRITE_TOOLS:
                continue  # silently drop invalid tools
            try:
                candidate = ActionCandidate(
                    tool_name=tool_name,
                    rationale_kr=str(item.get("rationale_kr", ""))[:500],
                    parameters_preview=dict(item.get("parameters_preview", {})),
                    severity=str(item.get("severity", "info")),
                    requires_human_approval=True,  # forced — never trust input
                    executed=False,                 # forced — agent never executes
                )
            except (RiskAgentError, ValueError, TypeError):
                continue
            out.append(candidate)
        return tuple(out)

    def _audit_emit(self, report: RiskAgentReport) -> None:
        """Best-effort audit emission. Agent must not crash on audit failure."""
        if self._audit is None:
            return
        try:
            audit_method = getattr(self._audit, "write_event", None)
            if callable(audit_method):
                audit_method(
                    event_type="risk_agent_report_emitted",
                    payload=report.to_audit_dict(),
                )
        except Exception:  # noqa: BLE001 — audit must never break agent
            pass


__all__ = (
    "RiskExplanationAgent",
    "RiskAgentInput",
    "RiskAgentReport",
    "ActionCandidate",
    "RiskAgentError",
    "SEVERITY_LEVELS",
    "SUGGESTABLE_WRITE_TOOLS",
    "ALLOWED_TOOLS",
    "MAX_ACTION_CANDIDATES",
)
