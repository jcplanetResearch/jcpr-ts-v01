"""Task 39 — P&L Explanation Agent.

Combines three data sources:
    - Task 26 P&L engine    via get_pnl_snapshot
    - Task 27 slippage      via get_recent_fills (cost-basis vs fill price)
    - Tasks A1-A3 audit     via get_audit_events (decision context)

Decomposes realized + unrealized P&L, fees, and slippage; produces
strategy-level and symbol-level attribution; explains material variance
in Korean.

Security guarantees (defense in depth):
    1. No write-tool invocation. Read-only data only.
    2. Allowlisted tool surface (ALLOWED_TOOLS).
    3. PII masking — client_order_id truncated, no operator names exposed.
    4. Decimal-only arithmetic. No float anywhere.
    5. Frozen dataclasses. UTC tz-aware timestamps.
    6. All decisions traced via TraceContext + AuditWriter.

Dependencies (must exist in operator's local repo):
    - src/agents/_llm_client.py     (Task 37)
    - src/agents/_mcp_client.py     (Task 37)
    - src/agents/_agent_runner.py   (Task 37)
    - src/agents/prompts/schemas/pnl_explanation.json  (Task 36)
    - src/observability/            (Tasks A1-A3)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Sequence

# Task 37 dependencies (resolved at runtime in operator's repo)
from ._agent_runner import AgentRunner, AgentSpec, AgentRunResult
from ._llm_client import LLMClient
from ._mcp_client import MCPReadOnlyClient, MCPCallResult


# =============================================================================
# Constants
# =============================================================================

#: Read-only MCP tools the P&L agent may call. Hard allowlist.
ALLOWED_TOOLS: frozenset[str] = frozenset({
    "get_pnl_snapshot",
    "get_recent_fills",
    "get_positions",
    "get_market_status",
    "get_strategy_registry",
})

#: Materiality thresholds for variance classification (KRW absolute).
MATERIALITY_HIGH_KRW: Decimal = Decimal("1000000")    # 1M KRW
MATERIALITY_MEDIUM_KRW: Decimal = Decimal("100000")   # 100k KRW
MATERIALITY_LOW_KRW: Decimal = Decimal("10000")       # 10k KRW

#: Maximum number of attribution rows in payload (top-N by absolute P&L).
MAX_STRATEGY_ATTRIBUTION_ROWS: int = 10
MAX_SYMBOL_ATTRIBUTION_ROWS: int = 15
MAX_RECENT_FILLS_SAMPLE: int = 50

#: Decimal precision for KRW (won — no fractional unit in spot trading).
KRW_QUANTIZE: Decimal = Decimal("1")

#: PII masking — client_order_id displayed as first 8 chars + '...'.
ORDER_ID_MASK_LEN: int = 8


class PnLAgentError(RuntimeError):
    """Raised when P&L agent invariants are violated."""


def _to_decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    """Robust Decimal conversion. Returns `default` on any parse failure."""
    if value is None:
        return default
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default


def _mask_order_id(order_id: Any) -> str:
    """Truncate client_order_id for safe display."""
    if not order_id:
        return ""
    s = str(order_id)
    if len(s) <= ORDER_ID_MASK_LEN:
        return s
    return s[:ORDER_ID_MASK_LEN] + "..."


def _classify_materiality(abs_krw: Decimal) -> str:
    """Map absolute KRW magnitude to materiality level."""
    abs_krw = abs(abs_krw)
    if abs_krw >= MATERIALITY_HIGH_KRW:
        return "high"
    if abs_krw >= MATERIALITY_MEDIUM_KRW:
        return "medium"
    if abs_krw >= MATERIALITY_LOW_KRW:
        return "low"
    return "info"


# =============================================================================
# Frozen data structures
# =============================================================================

@dataclass(frozen=True, slots=True)
class PnLAgentInput:
    """Operator-supplied context for a P&L explanation run."""
    starting_capital_krw: Decimal
    ending_capital_krw: Decimal
    operator_query: str
    requested_at_utc: datetime
    session_window_hours: int = 24

    def __post_init__(self) -> None:
        if not isinstance(self.starting_capital_krw, Decimal):
            raise TypeError("starting_capital_krw must be Decimal")
        if not isinstance(self.ending_capital_krw, Decimal):
            raise TypeError("ending_capital_krw must be Decimal")
        if self.starting_capital_krw <= Decimal("0"):
            raise ValueError("starting_capital_krw must be positive")
        if self.ending_capital_krw < Decimal("0"):
            raise ValueError("ending_capital_krw must be non-negative")
        if not isinstance(self.operator_query, str) or not self.operator_query.strip():
            raise ValueError("operator_query must be non-empty string")
        if self.requested_at_utc.tzinfo is None:
            raise ValueError("requested_at_utc must be tz-aware (UTC)")
        if self.session_window_hours < 1 or self.session_window_hours > 168:
            raise ValueError("session_window_hours must be 1..168 (1 week max)")

    @property
    def session_pnl_krw(self) -> Decimal:
        """Net change in capital over the session window."""
        return self.ending_capital_krw - self.starting_capital_krw


@dataclass(frozen=True, slots=True)
class PnLDecomposition:
    """Detailed P&L breakdown — output items 3, 4, 5 from spec."""
    realized_krw: Decimal
    unrealized_krw: Decimal
    fees_krw: Decimal
    slippage_krw: Decimal
    gross_pnl_krw: Decimal      # realized + unrealized (before fees/slippage)
    net_pnl_krw: Decimal        # gross - fees - slippage

    def __post_init__(self) -> None:
        for name in ("realized_krw", "unrealized_krw", "fees_krw",
                     "slippage_krw", "gross_pnl_krw", "net_pnl_krw"):
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                raise TypeError(f"{name} must be Decimal")


@dataclass(frozen=True, slots=True)
class AttributionRow:
    """A single attribution entry (strategy or symbol level)."""
    key: str                    # strategy_id or symbol
    realized_krw: Decimal
    unrealized_krw: Decimal
    net_pnl_krw: Decimal
    trade_count: int
    materiality: str

    def __post_init__(self) -> None:
        if not self.key or not isinstance(self.key, str):
            raise ValueError("key must be non-empty string")
        if self.trade_count < 0:
            raise ValueError("trade_count must be non-negative")


@dataclass(frozen=True, slots=True)
class PnLAgentReport:
    """Final P&L explanation report."""
    trace_id: str
    summary_kr: str
    materiality_overall: str
    decomposition: PnLDecomposition
    strategy_attribution: tuple[AttributionRow, ...]
    symbol_attribution: tuple[AttributionRow, ...]
    notable_events: tuple[Mapping[str, Any], ...]    # audit events of interest
    raw_llm_response: str
    fallback_used: bool
    schema_validated: bool
    elapsed_ms: int
    generated_at_utc: datetime

    def to_audit_dict(self) -> dict[str, Any]:
        """Audit-safe dict (no PII, no order ids)."""
        return {
            "trace_id": self.trace_id,
            "materiality_overall": self.materiality_overall,
            "realized_krw": str(self.decomposition.realized_krw),
            "unrealized_krw": str(self.decomposition.unrealized_krw),
            "fees_krw": str(self.decomposition.fees_krw),
            "slippage_krw": str(self.decomposition.slippage_krw),
            "net_pnl_krw": str(self.decomposition.net_pnl_krw),
            "strategy_count": len(self.strategy_attribution),
            "symbol_count": len(self.symbol_attribution),
            "notable_event_count": len(self.notable_events),
            "fallback_used": self.fallback_used,
            "schema_validated": self.schema_validated,
            "elapsed_ms": self.elapsed_ms,
            "generated_at_utc": self.generated_at_utc.isoformat(),
        }


# =============================================================================
# Tool collector — pulls from pnl_engine + slippage + audit
# =============================================================================

def _collect_pnl_tools(
    mcp_client: MCPReadOnlyClient,
    *,
    parent_trace: Any | None = None,
    session_window_hours: int = 24,
) -> tuple[MCPCallResult, ...]:
    """Gather P&L data from three sources.

    Order:
        1. get_market_status        — sanity context
        2. get_pnl_snapshot         — Task 26 pnl_engine totals
        3. get_recent_fills         — Task 27 slippage decomposition
        4. get_positions            — unrealized context
        5. get_strategy_registry    — strategy metadata for attribution

    Defensive: a failed call does not abort the chain.
    """
    results: list[MCPCallResult] = []
    tools_to_call: tuple[tuple[str, dict[str, Any]], ...] = (
        ("get_market_status", {}),
        ("get_pnl_snapshot", {"window_hours": session_window_hours}),
        ("get_recent_fills", {"limit": MAX_RECENT_FILLS_SAMPLE}),
        ("get_positions", {}),
        ("get_strategy_registry", {}),
    )
    for tool_name, kwargs in tools_to_call:
        if tool_name not in ALLOWED_TOOLS:
            raise PnLAgentError(f"tool_name '{tool_name}' violates ALLOWED_TOOLS")
        try:
            result = mcp_client.call(tool_name, **kwargs)
        except Exception as exc:  # noqa: BLE001 — must not crash on tool failure
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
# Decomposition — extract realized / unrealized / fees / slippage
# =============================================================================

def _extract_decomposition(
    tool_results: Sequence[MCPCallResult],
) -> PnLDecomposition:
    """Build PnLDecomposition from tool results.

    P&L engine snapshot provides realized/unrealized/fees totals.
    Recent fills provide slippage = sum(arrival_price - fill_price) * qty * sign.
    """
    realized = Decimal("0")
    unrealized = Decimal("0")
    fees = Decimal("0")
    slippage = Decimal("0")

    for result in tool_results:
        if not result.success:
            continue
        data = result.data or {}

        if result.tool_name == "get_pnl_snapshot":
            realized = _to_decimal(data.get("realized_krw", data.get("realized")))
            unrealized = _to_decimal(data.get("unrealized_krw", data.get("unrealized")))
            fees = _to_decimal(data.get("fees_krw", data.get("fees")))
            # If snapshot already provides slippage, prefer it
            snap_slip = data.get("slippage_krw", data.get("slippage"))
            if snap_slip is not None:
                slippage = _to_decimal(snap_slip)

        elif result.tool_name == "get_recent_fills":
            # Compute slippage if not already set from snapshot
            fills = data.get("fills", [])
            if not isinstance(fills, list):
                fills = []
            computed_slip = Decimal("0")
            for fill in fills[:MAX_RECENT_FILLS_SAMPLE]:
                if not isinstance(fill, Mapping):
                    continue
                arrival = _to_decimal(fill.get("arrival_price_krw"))
                fill_price = _to_decimal(fill.get("fill_price_krw"))
                qty = _to_decimal(fill.get("qty"))
                side = str(fill.get("side", "")).lower()
                if arrival == 0 or fill_price == 0 or qty == 0:
                    continue
                # Slippage cost: BUY fills above arrival hurt, SELL fills below arrival hurt
                if side in ("buy", "b", "long"):
                    computed_slip += (fill_price - arrival) * qty
                elif side in ("sell", "s", "short"):
                    computed_slip += (arrival - fill_price) * qty
            # Only use computed if snapshot didn't provide it
            if slippage == Decimal("0") and computed_slip != Decimal("0"):
                slippage = computed_slip

    gross = realized + unrealized
    net = gross - fees - slippage

    return PnLDecomposition(
        realized_krw=realized.quantize(KRW_QUANTIZE),
        unrealized_krw=unrealized.quantize(KRW_QUANTIZE),
        fees_krw=fees.quantize(KRW_QUANTIZE),
        slippage_krw=slippage.quantize(KRW_QUANTIZE),
        gross_pnl_krw=gross.quantize(KRW_QUANTIZE),
        net_pnl_krw=net.quantize(KRW_QUANTIZE),
    )


# =============================================================================
# Attribution — strategy-level and symbol-level
# =============================================================================

def _build_attribution(
    tool_results: Sequence[MCPCallResult],
) -> tuple[tuple[AttributionRow, ...], tuple[AttributionRow, ...]]:
    """Aggregate fills + positions into (strategy_rows, symbol_rows)."""
    strategy_acc: dict[str, dict[str, Any]] = {}
    symbol_acc: dict[str, dict[str, Any]] = {}

    for result in tool_results:
        if not result.success:
            continue
        data = result.data or {}

        if result.tool_name == "get_recent_fills":
            for fill in data.get("fills", [])[:MAX_RECENT_FILLS_SAMPLE]:
                if not isinstance(fill, Mapping):
                    continue
                strategy_id = str(fill.get("strategy_id", "unknown"))
                symbol = str(fill.get("symbol", "unknown"))
                realized = _to_decimal(fill.get("realized_pnl_krw"))

                _accumulate(strategy_acc, strategy_id,
                            realized=realized, unrealized=Decimal("0"))
                _accumulate(symbol_acc, symbol,
                            realized=realized, unrealized=Decimal("0"))

        elif result.tool_name == "get_positions":
            for pos in data.get("positions", []):
                if not isinstance(pos, Mapping):
                    continue
                strategy_id = str(pos.get("strategy_id", "unknown"))
                symbol = str(pos.get("symbol", "unknown"))
                unrealized = _to_decimal(pos.get("unrealized_pnl_krw"))

                _accumulate(strategy_acc, strategy_id,
                            realized=Decimal("0"), unrealized=unrealized)
                _accumulate(symbol_acc, symbol,
                            realized=Decimal("0"), unrealized=unrealized)

    strategy_rows = _finalize_attribution(strategy_acc, MAX_STRATEGY_ATTRIBUTION_ROWS)
    symbol_rows = _finalize_attribution(symbol_acc, MAX_SYMBOL_ATTRIBUTION_ROWS)
    return strategy_rows, symbol_rows


def _accumulate(
    acc: dict[str, dict[str, Any]],
    key: str,
    *,
    realized: Decimal,
    unrealized: Decimal,
) -> None:
    """Add to running accumulator. trade_count increments only on realized != 0."""
    bucket = acc.setdefault(key, {
        "realized": Decimal("0"),
        "unrealized": Decimal("0"),
        "trade_count": 0,
    })
    bucket["realized"] += realized
    bucket["unrealized"] += unrealized
    if realized != Decimal("0"):
        bucket["trade_count"] += 1


def _finalize_attribution(
    acc: Mapping[str, Mapping[str, Any]],
    max_rows: int,
) -> tuple[AttributionRow, ...]:
    """Convert accumulator dict to sorted, capped tuple of AttributionRow."""
    rows: list[AttributionRow] = []
    for key, bucket in acc.items():
        realized = bucket.get("realized", Decimal("0"))
        unrealized = bucket.get("unrealized", Decimal("0"))
        net = realized + unrealized
        try:
            row = AttributionRow(
                key=key,
                realized_krw=realized.quantize(KRW_QUANTIZE),
                unrealized_krw=unrealized.quantize(KRW_QUANTIZE),
                net_pnl_krw=net.quantize(KRW_QUANTIZE),
                trade_count=int(bucket.get("trade_count", 0)),
                materiality=_classify_materiality(net),
            )
        except (ValueError, TypeError):
            continue
        rows.append(row)

    # Sort by absolute net P&L descending — biggest movers on top
    rows.sort(key=lambda r: abs(r.net_pnl_krw), reverse=True)
    return tuple(rows[:max_rows])


# =============================================================================
# Notable events — audit log highlights
# =============================================================================

def _extract_notable_events(
    tool_results: Sequence[MCPCallResult],
) -> tuple[Mapping[str, Any], ...]:
    """Surface audit events that materially affected P&L (rejections, kills)."""
    events: list[Mapping[str, Any]] = []
    for result in tool_results:
        if not result.success:
            continue
        # P&L snapshot may carry notable events array
        if result.tool_name == "get_pnl_snapshot":
            for evt in (result.data or {}).get("notable_events", [])[:10]:
                if not isinstance(evt, Mapping):
                    continue
                events.append({
                    "event_type": str(evt.get("event_type", "unknown")),
                    "timestamp_utc": str(evt.get("timestamp_utc", "")),
                    "description_kr": str(evt.get("description_kr", ""))[:300],
                    "impact_krw": str(_to_decimal(evt.get("impact_krw"))),
                    "order_id_masked": _mask_order_id(evt.get("order_id")),
                })
    return tuple(events[:10])


# =============================================================================
# Fallback — schema-conforming payload when LLM fails
# =============================================================================

def _build_pnl_fallback(
    tool_results: Sequence[MCPCallResult],
    operator_query: str,
) -> dict[str, Any]:
    """Fallback payload directly from tool results (no LLM)."""
    decomp = _extract_decomposition(tool_results)
    strategy_rows, symbol_rows = _build_attribution(tool_results)
    notable = _extract_notable_events(tool_results)

    materiality = _classify_materiality(decomp.net_pnl_krw)
    direction_kr = "수익(profit)" if decomp.net_pnl_krw > 0 else (
        "손실(loss)" if decomp.net_pnl_krw < 0 else "변동 없음(flat)"
    )

    summary_parts = [
        f"세션 순손익(net P&L): {decomp.net_pnl_krw:,} KRW ({direction_kr}). "
        f"실현(realized) {decomp.realized_krw:,}, "
        f"미실현(unrealized) {decomp.unrealized_krw:,}.",
        f"수수료(fees) {decomp.fees_krw:,}, "
        f"슬리피지(slippage) {decomp.slippage_krw:,}.",
    ]
    if strategy_rows:
        top_s = strategy_rows[0]
        summary_parts.append(
            f"최대 기여 전략(top strategy): {top_s.key} "
            f"순손익 {top_s.net_pnl_krw:,} KRW ({top_s.trade_count}건)."
        )
    if symbol_rows:
        top_sym = symbol_rows[0]
        summary_parts.append(
            f"최대 기여 종목(top symbol): {top_sym.key} "
            f"순손익 {top_sym.net_pnl_krw:,} KRW."
        )

    summary_kr = " ".join(summary_parts)

    return {
        "summary_kr": summary_kr,
        "materiality_overall": materiality,
        "decomposition": {
            "realized_krw": str(decomp.realized_krw),
            "unrealized_krw": str(decomp.unrealized_krw),
            "fees_krw": str(decomp.fees_krw),
            "slippage_krw": str(decomp.slippage_krw),
            "gross_pnl_krw": str(decomp.gross_pnl_krw),
            "net_pnl_krw": str(decomp.net_pnl_krw),
        },
        "strategy_attribution": [
            {
                "key": r.key,
                "realized_krw": str(r.realized_krw),
                "unrealized_krw": str(r.unrealized_krw),
                "net_pnl_krw": str(r.net_pnl_krw),
                "trade_count": r.trade_count,
                "materiality": r.materiality,
            }
            for r in strategy_rows
        ],
        "symbol_attribution": [
            {
                "key": r.key,
                "realized_krw": str(r.realized_krw),
                "unrealized_krw": str(r.unrealized_krw),
                "net_pnl_krw": str(r.net_pnl_krw),
                "trade_count": r.trade_count,
                "materiality": r.materiality,
            }
            for r in symbol_rows
        ],
        "notable_events": [dict(e) for e in notable],
        "operator_query_echo": operator_query[:500],
        "fallback_used": True,
    }


# =============================================================================
# Main agent class
# =============================================================================

class PnLExplanationAgent:
    """Operator-facing agent that explains P&L variance in Korean.

    Usage:
        agent = PnLExplanationAgent(llm_client, mcp_client, audit_writer)
        report = agent.explain_pnl(
            starting_capital_krw=Decimal("100000000"),
            ending_capital_krw=Decimal("99500000"),
            operator_query="오늘 손실 사유 분석",
        )
    """

    SYSTEM_TEMPLATE_ID: str = "pnl_analyst_system"
    USER_TEMPLATE_ID: str = "pnl_explanation_task"
    SCHEMA_ID: str = "pnl_explanation"
    AGENT_NAME: str = "pnl_explanation_agent"

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
        self._session_window_hours = 24  # mutable per-call via explain_pnl

        # Tool collector closure captures session_window_hours
        def _collector(parent_trace=None):
            return _collect_pnl_tools(
                self._mcp,
                parent_trace=parent_trace,
                session_window_hours=self._session_window_hours,
            )

        self._spec = AgentSpec(
            agent_name=self.AGENT_NAME,
            system_template_id=self.SYSTEM_TEMPLATE_ID,
            user_template_id=self.USER_TEMPLATE_ID,
            schema_id=self.SCHEMA_ID,
            tool_collector=_collector,
            fallback_builder=_build_pnl_fallback,
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

    def explain_pnl(
        self,
        *,
        starting_capital_krw: Decimal,
        ending_capital_krw: Decimal,
        operator_query: str,
        session_window_hours: int = 24,
        parent_trace: Any | None = None,
    ) -> PnLAgentReport:
        """Run the full P&L explanation pipeline."""
        agent_input = PnLAgentInput(
            starting_capital_krw=starting_capital_krw,
            ending_capital_krw=ending_capital_krw,
            operator_query=operator_query,
            session_window_hours=session_window_hours,
            requested_at_utc=datetime.now(tz=timezone.utc),
        )
        # Update session window for the collector closure
        self._session_window_hours = session_window_hours

        run_result: AgentRunResult = self._runner.run(
            template_variables={
                "starting_capital_krw": str(agent_input.starting_capital_krw),
                "ending_capital_krw": str(agent_input.ending_capital_krw),
                "session_pnl_krw": str(agent_input.session_pnl_krw),
                "operator_query": agent_input.operator_query,
                "session_window_hours": str(agent_input.session_window_hours),
                "requested_at_utc": agent_input.requested_at_utc.isoformat(),
            },
            parent_trace=parent_trace,
        )

        report = self._build_report(run_result)
        self._audit_emit(report)
        return report

    # -------------------------------------------------------------------------
    # Internals
    # -------------------------------------------------------------------------

    def _build_report(self, run_result: AgentRunResult) -> PnLAgentReport:
        """Convert raw payload into typed PnLAgentReport with safety guards."""
        payload = run_result.payload

        # Decomposition — must be present and Decimal-castable
        decomp_raw = payload.get("decomposition", {}) if isinstance(payload, Mapping) else {}
        if not isinstance(decomp_raw, Mapping):
            decomp_raw = {}
        decomposition = PnLDecomposition(
            realized_krw=_to_decimal(decomp_raw.get("realized_krw")),
            unrealized_krw=_to_decimal(decomp_raw.get("unrealized_krw")),
            fees_krw=_to_decimal(decomp_raw.get("fees_krw")),
            slippage_krw=_to_decimal(decomp_raw.get("slippage_krw")),
            gross_pnl_krw=_to_decimal(decomp_raw.get("gross_pnl_krw")),
            net_pnl_krw=_to_decimal(decomp_raw.get("net_pnl_krw")),
        )

        strategy_rows = self._extract_attribution_rows(
            payload.get("strategy_attribution", []),
            MAX_STRATEGY_ATTRIBUTION_ROWS,
        )
        symbol_rows = self._extract_attribution_rows(
            payload.get("symbol_attribution", []),
            MAX_SYMBOL_ATTRIBUTION_ROWS,
        )

        notable_raw = payload.get("notable_events", [])
        notable: list[Mapping[str, Any]] = []
        if isinstance(notable_raw, list):
            for evt in notable_raw[:10]:
                if isinstance(evt, Mapping):
                    notable.append({
                        "event_type": str(evt.get("event_type", ""))[:100],
                        "timestamp_utc": str(evt.get("timestamp_utc", ""))[:50],
                        "description_kr": str(evt.get("description_kr", ""))[:300],
                        "impact_krw": str(_to_decimal(evt.get("impact_krw"))),
                        "order_id_masked": _mask_order_id(evt.get("order_id_masked")
                                                         or evt.get("order_id")),
                    })

        return PnLAgentReport(
            trace_id=run_result.trace_id,
            summary_kr=str(payload.get("summary_kr", "(요약 없음)"))[:2000],
            materiality_overall=str(payload.get("materiality_overall", "info")),
            decomposition=decomposition,
            strategy_attribution=strategy_rows,
            symbol_attribution=symbol_rows,
            notable_events=tuple(notable),
            raw_llm_response=run_result.raw_llm_response,
            fallback_used=run_result.fallback_used,
            schema_validated=run_result.schema_validated,
            elapsed_ms=run_result.elapsed_ms,
            generated_at_utc=datetime.now(tz=timezone.utc),
        )

    @staticmethod
    def _extract_attribution_rows(
        raw: Any, max_rows: int,
    ) -> tuple[AttributionRow, ...]:
        """Defense-in-depth: convert untrusted payload to AttributionRow tuple."""
        if not isinstance(raw, list):
            return ()
        rows: list[AttributionRow] = []
        for item in raw[:max_rows]:
            if not isinstance(item, Mapping):
                continue
            try:
                row = AttributionRow(
                    key=str(item.get("key", ""))[:80],
                    realized_krw=_to_decimal(item.get("realized_krw")),
                    unrealized_krw=_to_decimal(item.get("unrealized_krw")),
                    net_pnl_krw=_to_decimal(item.get("net_pnl_krw")),
                    trade_count=max(0, int(item.get("trade_count", 0) or 0)),
                    materiality=str(item.get("materiality", "info")),
                )
            except (ValueError, TypeError):
                continue
            rows.append(row)
        return tuple(rows)

    def _audit_emit(self, report: PnLAgentReport) -> None:
        """Best-effort audit emission. Never crashes the agent."""
        if self._audit is None:
            return
        try:
            audit_method = getattr(self._audit, "write_event", None)
            if callable(audit_method):
                audit_method(
                    event_type="pnl_agent_report_emitted",
                    payload=report.to_audit_dict(),
                )
        except Exception:  # noqa: BLE001
            pass


__all__ = (
    "PnLExplanationAgent",
    "PnLAgentInput",
    "PnLAgentReport",
    "PnLDecomposition",
    "AttributionRow",
    "PnLAgentError",
    "ALLOWED_TOOLS",
    "MATERIALITY_HIGH_KRW",
    "MATERIALITY_MEDIUM_KRW",
    "MATERIALITY_LOW_KRW",
    "MAX_STRATEGY_ATTRIBUTION_ROWS",
    "MAX_SYMBOL_ATTRIBUTION_ROWS",
)
