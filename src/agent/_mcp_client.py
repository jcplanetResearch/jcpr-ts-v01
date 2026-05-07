"""
MCP In-Process 클라이언트 (MCP In-Process Client)
==================================================

JCPR Trading System - jcpr-ts-v01
Task 37 v0.1

Task 34 read-only MCP server를 in-process 함수로 호출.
(Calls Task 34 read-only MCP tools as in-process functions.)

설계 (Design):
    - stdio 프로토콜 우회 — _tool_handlers 함수 직접 호출
    - 각 호출은 자체 trace_id 생성 + audit 기록
    - 출력은 Task 34와 동일 (mask_output 적용됨)
    - read-only 보장 — write_handlers는 절대 호출 안 함

도구 8개 (Same as Task 34 readonly_server):
    1. get_market_status
    2. get_positions
    3. get_pnl_snapshot
    4. get_recent_fills
    5. get_rejection_summary
    6. get_portfolio_risk
    7. get_strategy_registry
    8. get_trace
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from src.mcp_servers import (
    ReadOnlyServerConfig,
    load_config_from_env,
)
from src.mcp_servers import _tool_handlers as th
from src.observability import (
    AuditWriter,
    ORIGIN_AGENT,
    TraceContext,
    get_default_writer,
)


logger = logging.getLogger("jcpr.agents.mcp_client")


# ─────────────────────────────────────────────────
# 결과 포장 (Result Wrapper)
# ─────────────────────────────────────────────────

@dataclass(frozen=True)
class MCPCallResult:
    """MCP 호출 결과."""

    tool_name: str
    success: bool
    data: dict[str, Any]
    trace_id: str
    elapsed_ms: float
    called_at_utc: datetime

    def __post_init__(self):
        if self.called_at_utc.tzinfo is None:
            raise ValueError("called_at_utc must be tz-aware")

    @property
    def error_code(self) -> Optional[str]:
        if self.success:
            return None
        return self.data.get("error_code")

    @property
    def error_message(self) -> Optional[str]:
        if self.success:
            return None
        return self.data.get("error_message")


# ─────────────────────────────────────────────────
# 클라이언트
# ─────────────────────────────────────────────────

@dataclass
class MCPReadOnlyClient:
    """
    Task 34 read-only 도구를 in-process 호출.

    Args:
        config: ReadOnlyServerConfig (None이면 env에서 로드)
        agent_name: agent 식별자 (audit 기록용)
        parent_trace: 상위 trace context (있으면 자식 span 생성)
    """

    config: Optional[ReadOnlyServerConfig] = None
    agent_name: str = "agent"
    parent_trace: Optional[TraceContext] = None
    _writer: Optional[AuditWriter] = field(default=None, init=False, repr=False)

    def __post_init__(self):
        if self.config is None:
            self.config = load_config_from_env()
        self._writer = get_default_writer()

    # ─────────────────────────────────────────
    # 공통 호출 wrapper
    # ─────────────────────────────────────────

    def _call(self, tool_name: str, handler_fn, **kwargs) -> MCPCallResult:
        """공통: trace 생성 → handler 호출 → audit → 결과 포장."""
        # Trace 생성 (parent 있으면 child span)
        if self.parent_trace:
            ctx = self.parent_trace.child_span(
                f"mcp.{tool_name}",
                additional_correlation={"agent": self.agent_name},
            )
        else:
            ctx = TraceContext.new(
                origin=ORIGIN_AGENT,
                operator_id=self.agent_name,
                session_id=self.config.session_id,
                correlation_keys={
                    "tool": tool_name,
                    "agent": self.agent_name,
                    "transport": "in_process",
                },
            )

        start = datetime.now(timezone.utc)

        # Audit 시작
        if self._writer:
            self._writer.write_mcp_call(
                ctx,
                payload={
                    "tool": tool_name,
                    "args_keys": list(kwargs.keys()),
                    "transport": "in_process",
                },
            )

        # Handler 호출
        try:
            result = handler_fn(self.config, **kwargs)
        except Exception as e:  # noqa: BLE001
            logger.exception(f"in-process MCP call failed: {tool_name}")
            if self._writer:
                self._writer.write_exception(
                    ctx, e,
                    additional={"tool": tool_name},
                )
            result = {
                "ok": False,
                "error_code": "HANDLER_ERROR",
                "error_message": f"{type(e).__name__}: {e}",
            }

        elapsed_ms = (
            datetime.now(timezone.utc) - start
        ).total_seconds() * 1000.0

        # Audit 결과
        if self._writer:
            self._writer.write_mcp_result(
                ctx,
                payload={
                    "tool": tool_name,
                    "ok": result.get("ok"),
                    "elapsed_ms": elapsed_ms,
                    "error_code": result.get("error_code"),
                },
            )

        return MCPCallResult(
            tool_name=tool_name,
            success=bool(result.get("ok")),
            data=result,
            trace_id=ctx.trace_id,
            elapsed_ms=elapsed_ms,
            called_at_utc=start,
        )

    # ─────────────────────────────────────────
    # 8개 도구 (Task 34와 동일)
    # ─────────────────────────────────────────

    def get_market_status(self) -> MCPCallResult:
        return self._call("get_market_status", th.get_market_status)

    def get_positions(self) -> MCPCallResult:
        return self._call("get_positions", th.get_positions)

    def get_pnl_snapshot(
        self,
        starting_capital_krw: str,
        cash_krw: str,
    ) -> MCPCallResult:
        return self._call(
            "get_pnl_snapshot", th.get_pnl_snapshot,
            starting_capital_krw=starting_capital_krw,
            cash_krw=cash_krw,
        )

    def get_recent_fills(
        self,
        limit: int = 50,
        since_iso: Optional[str] = None,
    ) -> MCPCallResult:
        return self._call(
            "get_recent_fills", th.get_recent_fills,
            limit=limit, since_iso=since_iso,
        )

    def get_rejection_summary(
        self,
        since_iso: Optional[str] = None,
    ) -> MCPCallResult:
        return self._call(
            "get_rejection_summary", th.get_rejection_summary,
            since_iso=since_iso,
        )

    def get_portfolio_risk(
        self,
        sector_map: dict[str, str],
        cash_krw: str,
    ) -> MCPCallResult:
        return self._call(
            "get_portfolio_risk", th.get_portfolio_risk,
            sector_map=sector_map, cash_krw=cash_krw,
        )

    def get_strategy_registry(self) -> MCPCallResult:
        return self._call("get_strategy_registry", th.get_strategy_registry)

    def get_trace(
        self,
        trace_id: str,
        include_tree: bool = True,
    ) -> MCPCallResult:
        return self._call(
            "get_trace", th.get_trace,
            trace_id=trace_id, include_tree=include_tree,
        )

    # ─────────────────────────────────────────
    # 일반화된 호출 (도구 이름 → 함수 매핑)
    # ─────────────────────────────────────────

    def call(self, tool_name: str, **kwargs) -> MCPCallResult:
        """동적 도구 호출 — agent_runner 가 사용."""
        mapping = {
            "get_market_status": self.get_market_status,
            "get_positions": self.get_positions,
            "get_pnl_snapshot": self.get_pnl_snapshot,
            "get_recent_fills": self.get_recent_fills,
            "get_rejection_summary": self.get_rejection_summary,
            "get_portfolio_risk": self.get_portfolio_risk,
            "get_strategy_registry": self.get_strategy_registry,
            "get_trace": self.get_trace,
        }
        if tool_name not in mapping:
            return MCPCallResult(
                tool_name=tool_name,
                success=False,
                data={
                    "ok": False,
                    "error_code": "UNKNOWN_TOOL",
                    "error_message": f"tool {tool_name!r} not in read-only set",
                },
                trace_id="",
                elapsed_ms=0.0,
                called_at_utc=datetime.now(timezone.utc),
            )
        return mapping[tool_name](**kwargs)


__all__ = [
    "MCPCallResult",
    "MCPReadOnlyClient",
]
