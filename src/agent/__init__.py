"""
에이전트 패키지 (Agents Package)
=================================

JCPR Trading System - jcpr-ts-v01

Task 36 v0.1 — Prompt templates (LLM-agnostic)
Task 37 (예정) — market_agent
Task 38 (예정) — risk_agent
Task 39 (예정) — pnl_agent
"""

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
]

__version__ = "0.1.0"
