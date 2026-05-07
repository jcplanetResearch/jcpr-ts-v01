"""
프롬프트 패키지 (Prompts Package)
==================================

JCPR Trading System - jcpr-ts-v01
Task 36 v0.1
"""

from ._audit import (
    write_agent_decision,
    write_agent_prompt,
    write_agent_response,
)
from ._registry import (
    DEFAULT_PROMPT_ROOT,
    PromptRegistry,
    RegistryError,
    TemplateLoadError,
    TemplateNotFound,
    get_default_registry,
    reset_default_registry,
)
from ._renderer import (
    MAX_SINGLE_VALUE_BYTES,
    MAX_TOTAL_RENDERED_BYTES,
    MAX_VARIABLE_COUNT,
    RenderError,
    SecretInVariableError,
    ValueTooLargeError,
    VariableMissingError,
    safe_render,
)
from ._template import (
    AGENT_COMMON,
    AGENT_MARKET_ANALYST,
    AGENT_PNL_EXPLAINER,
    AGENT_RISK_EXPLAINER,
    ALLOWED_AGENTS,
    ALLOWED_ROLES,
    PromptTemplate,
    RenderedPrompt,
    ROLE_SYSTEM,
    ROLE_TOOL_GUIDE,
    ROLE_USER,
    extract_variables,
)

__all__ = [
    # Template model
    "PromptTemplate",
    "RenderedPrompt",
    "extract_variables",
    "ROLE_SYSTEM", "ROLE_USER", "ROLE_TOOL_GUIDE",
    "ALLOWED_ROLES",
    "AGENT_MARKET_ANALYST", "AGENT_RISK_EXPLAINER",
    "AGENT_PNL_EXPLAINER", "AGENT_COMMON",
    "ALLOWED_AGENTS",
    # Renderer
    "safe_render",
    "RenderError", "SecretInVariableError",
    "VariableMissingError", "ValueTooLargeError",
    "MAX_SINGLE_VALUE_BYTES",
    "MAX_TOTAL_RENDERED_BYTES",
    "MAX_VARIABLE_COUNT",
    # Registry
    "PromptRegistry",
    "RegistryError", "TemplateNotFound", "TemplateLoadError",
    "get_default_registry", "reset_default_registry",
    "DEFAULT_PROMPT_ROOT",
    # Audit
    "write_agent_prompt",
    "write_agent_response",
    "write_agent_decision",
]

__version__ = "0.1.0"
