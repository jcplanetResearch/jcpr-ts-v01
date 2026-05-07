"""
에이전트 패키지 (Agents Package)
=================================

JCPR Trading System - jcpr-ts-v01

Task 36 v0.1 — Prompt templates (LLM-agnostic)
Task 37 v0.1 — market_agent (Market Analyst Agent)
Task 38 (예정) — risk_agent
Task 39 (예정) — pnl_agent
"""

from ._agent_runner import (
    AgentContext,
    AgentRunResult,
    AgentRunner,
    AgentSpec,
    FallbackFn,
    ToolCollectorFn,
)
from ._llm_client import (
    LLMClient,
    LLMRequest,
    LLMResponse,
    MockLLMClient,
    parse_json_response,
    validate_response,
)
from ._mcp_client import (
    MCPCallResult,
    MCPReadOnlyClient,
)
from .market_agent import MarketAnalystAgent
from .prompts import (
    AGENT_COMMON,
    AGENT_MARKET_ANALYST,
    AGENT_PNL_EXPLAINER,
    AGENT_RISK_EXPLAINER,
    PromptRegistry,
    PromptTemplate,
    RenderedPrompt,
    get_default_registry,
    safe_render,
    write_agent_decision,
    write_agent_prompt,
    write_agent_response,
)

__all__ = [
    # Task 36 (prompts)
    "PromptRegistry",
    "PromptTemplate",
    "RenderedPrompt",
    "safe_render",
    "get_default_registry",
    "write_agent_prompt",
    "write_agent_response",
    "write_agent_decision",
    "AGENT_MARKET_ANALYST",
    "AGENT_RISK_EXPLAINER",
    "AGENT_PNL_EXPLAINER",
    "AGENT_COMMON",
    # Task 37 — LLM client
    "LLMRequest",
    "LLMResponse",
    "LLMClient",
    "MockLLMClient",
    "validate_response",
    "parse_json_response",
    # Task 37 — MCP client
    "MCPCallResult",
    "MCPReadOnlyClient",
    # Task 37 — Runner (재사용 가능)
    "AgentSpec",
    "AgentRunner",
    "AgentRunResult",
    "AgentContext",
    "ToolCollectorFn",
    "FallbackFn",
    # Task 37 — Market Agent
    "MarketAnalystAgent",
]

__version__ = "0.2.0"  # Task 36 + 37
