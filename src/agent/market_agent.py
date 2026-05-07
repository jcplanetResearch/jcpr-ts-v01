"""
Market Analyst Agent
=====================

JCPR Trading System - jcpr-ts-v01
Task 37 v0.1

운영자에게 시장 상황을 한국어로 요약하는 에이전트.
(Provides market summary in Korean to the operator.)

사용법 (Usage):
    from src.agents import MarketAnalystAgent
    from src.agents._llm_client import MockLLMClient

    agent = MarketAnalystAgent(
        llm_client=MockLLMClient(),
        operator_id="alice",
        session_id="2026-05-07",
    )

    result = agent.summarize_market(
        starting_capital_krw="10000000",
        cash_krw="500000",
        operator_query="how are we doing?",
    )

    print(result.summary_ko)        # 한국어 요약
    print(result.response)           # 구조화 dict (schema 검증됨)
    print(result.trace_id)           # trc-...
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional

from ._agent_runner import (
    AgentContext,
    AgentRunner,
    AgentRunResult,
    AgentSpec,
)
from ._llm_client import LLMClient
from ._mcp_client import MCPReadOnlyClient
from .prompts import (
    AGENT_MARKET_ANALYST,
    PromptRegistry,
    get_default_registry,
)


logger = logging.getLogger("jcpr.agents.market")


# ─────────────────────────────────────────────────
# 도구 수집 함수 (Tool Collector)
# ─────────────────────────────────────────────────

def _collect_market_tools(
    ctx: AgentContext,
    mcp: MCPReadOnlyClient,
    variables: dict[str, Any],
) -> None:
    """
    Market Analyst의 표준 도구 호출 순서:
        1. get_market_status — 장 상태
        2. get_positions — 포지션
        3. get_pnl_snapshot — P&L
        4. (필요 시) get_rejection_summary — 최근 거부 (있으면 알림)
        5. (필요 시) get_strategy_registry — 전략 메타
    """
    starting_capital = variables.get("starting_capital_krw", "0")
    cash = variables.get("cash_krw", "0")

    # 1. 시장 상태
    ctx.add(mcp.get_market_status())

    # 2. 포지션
    ctx.add(mcp.get_positions())

    # 3. P&L
    ctx.add(mcp.get_pnl_snapshot(
        starting_capital_krw=str(starting_capital),
        cash_krw=str(cash),
    ))

    # 4. 거부 (선택 — 정보용)
    rejection = mcp.get_rejection_summary()
    if rejection.success:
        # 거부가 있을 때만 LLM에 전달 (없으면 노이즈)
        rej_data = rejection.data or {}
        if rej_data.get("rejections", 0) > 0:
            ctx.add(rejection)

    # 5. 전략 (선택)
    ctx.add(mcp.get_strategy_registry())


# ─────────────────────────────────────────────────
# Fallback 응답 생성 (LLM 실패 시)
# ─────────────────────────────────────────────────

def _build_market_fallback(
    ctx: AgentContext,
    variables: dict[str, Any],
) -> dict[str, Any]:
    """
    LLM 응답 실패 시 도구 결과만으로 응답 생성.

    Schema (market_analysis.json) 준수:
        - summary_ko (필수)
        - findings (필수, list)
        - next_steps (선택)
        - uncertainties (선택)
    """
    findings: list[dict[str, Any]] = []

    market_state = "unknown"
    positions_count = 0
    pnl_text = "unknown"

    for r in ctx.tool_results:
        if not r.success:
            findings.append({
                "statement": f"{r.tool_name} failed: {r.error_code or 'error'}",
                "source": r.tool_name if r.tool_name in (
                    "get_market_status", "get_positions", "get_pnl_snapshot",
                    "get_recent_fills", "get_rejection_summary",
                    "get_portfolio_risk", "get_strategy_registry", "get_trace",
                ) else "computed",
                "confidence": "low",
            })
            continue

        d = r.data or {}

        if r.tool_name == "get_market_status":
            market_state = d.get("state", "unknown")
            findings.append({
                "statement": f"KRX 상태: {market_state}",
                "source": "get_market_status",
                "value": market_state,
                "confidence": "high",
            })

        elif r.tool_name == "get_positions":
            positions = d.get("positions", [])
            positions_count = len(positions)
            findings.append({
                "statement": f"보유 포지션 {positions_count}개",
                "source": "get_positions",
                "value": positions_count,
                "confidence": "high",
            })

        elif r.tool_name == "get_pnl_snapshot":
            pnl_krw = d.get("pnl_krw", "0")
            equity = d.get("equity_krw", "0")
            pnl_pct = d.get("pnl_pct", "0")
            pnl_text = f"P&L {pnl_krw} KRW ({pnl_pct}%)"
            findings.append({
                "statement": (
                    f"현재 자산 {equity} KRW, P&L {pnl_krw} KRW "
                    f"({pnl_pct}%)"
                ),
                "source": "get_pnl_snapshot",
                "value": str(pnl_krw),
                "confidence": "high",
            })

        elif r.tool_name == "get_rejection_summary":
            rej = d.get("rejections", 0)
            if rej > 0:
                findings.append({
                    "statement": f"⚠ 최근 거부된 주문 {rej}건",
                    "source": "get_rejection_summary",
                    "value": rej,
                    "confidence": "high",
                })

        elif r.tool_name == "get_strategy_registry":
            reg = d.get("registry", {})
            active = reg.get("active_count", 0)
            findings.append({
                "statement": f"활성 전략 {active}개",
                "source": "get_strategy_registry",
                "value": active,
                "confidence": "high",
            })

    summary_ko = (
        f"시장 상태: {market_state}. "
        f"포지션 {positions_count}개. "
        f"{pnl_text}. "
        f"(LLM 응답 실패 — 데이터 기반 fallback)"
    )

    response: dict[str, Any] = {
        "summary_ko": summary_ko[:1500],
        "findings": findings[:30],
        "uncertainties": [
            "LLM 응답 검증 실패 — 자동 생성된 요약입니다",
        ],
    }
    return response


# ─────────────────────────────────────────────────
# Agent 클래스
# ─────────────────────────────────────────────────

@dataclass
class MarketAnalystAgent:
    """
    시장 분석 에이전트 (Read-only).

    Tasks 34 (read-only MCP) + Task 36 (prompts) 결합.
    Task 35 (write) 도구는 절대 호출 안 함.

    Args:
        llm_client: LLMClient (Mock 또는 실제)
        operator_id: 운영자 ID
        session_id: 세션 ID
        prompt_registry: PromptRegistry (None이면 default)
        mcp_client: MCPReadOnlyClient (None이면 자동 생성)
    """

    llm_client: LLMClient
    operator_id: str = "operator-default"
    session_id: str = "session-default"
    prompt_registry: Optional[PromptRegistry] = None
    mcp_client: Optional[MCPReadOnlyClient] = None
    _runner: Optional[AgentRunner] = field(default=None, init=False, repr=False)

    def __post_init__(self):
        if self.prompt_registry is None:
            self.prompt_registry = get_default_registry()
        if self.mcp_client is None:
            self.mcp_client = MCPReadOnlyClient(agent_name="market_analyst")

        spec = AgentSpec(
            agent_name="market_analyst",
            system_template_id="market_analyst.system",
            user_template_id="market_analyst.market_summary",
            tool_collector=_collect_market_tools,
            fallback_builder=_build_market_fallback,
            max_tool_calls=10,
        )
        self._runner = AgentRunner(
            spec=spec,
            llm_client=self.llm_client,
            mcp_client=self.mcp_client,
            prompt_registry=self.prompt_registry,
            operator_id=self.operator_id,
            session_id=self.session_id,
        )

    # ─────────────────────────────────────────
    # 공개 API
    # ─────────────────────────────────────────

    def summarize_market(
        self,
        *,
        starting_capital_krw: str,
        cash_krw: str,
        operator_query: str = "",
    ) -> AgentRunResult:
        """
        시장 상황 요약.

        Args:
            starting_capital_krw: 세션 시작 자본 (Decimal-string)
            cash_krw: 현재 현금 (Decimal-string)
            operator_query: 운영자 자연어 질문 (선택)

        Returns:
            AgentRunResult
        """
        # 입력 검증
        for name, val in [
            ("starting_capital_krw", starting_capital_krw),
            ("cash_krw", cash_krw),
        ]:
            try:
                d = Decimal(str(val))
                if d < 0:
                    raise ValueError(f"{name} must be ≥ 0, got {d}")
            except Exception as e:
                raise ValueError(f"invalid {name}: {e}") from e

        return self._runner.run(
            system_variables={
                "session_id": self.session_id,
                "operator_id": self.operator_id,
            },
            user_variables={
                "starting_capital_krw": str(starting_capital_krw),
                "cash_krw": str(cash_krw),
            },
            operator_query=operator_query,
        )


__all__ = [
    "MarketAnalystAgent",
]
