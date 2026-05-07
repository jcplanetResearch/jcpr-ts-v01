"""
프롬프트 템플릿 (Prompt Template)
==================================

JCPR Trading System - jcpr-ts-v01
Task 36 v0.1

LLM-agnostic 프롬프트 템플릿 모델. 변수 자리표시자 + JSON schema 포함.
(LLM-agnostic prompt template with variable placeholders + JSON schema.)

설계 (Design):
    - frozen=True (불변)
    - 변수 자리표시자: {{ variable_name }} (Jinja-like, 단순)
    - 응답 schema 명시 (JSON schema)
    - target_agent 분류 (market_analyst/risk_explainer/pnl_explainer/common)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


# ─────────────────────────────────────────────────
# 상수 (Constants)
# ─────────────────────────────────────────────────

# Role 분류
ROLE_SYSTEM = "system"
ROLE_USER = "user"
ROLE_TOOL_GUIDE = "tool_guide"

ALLOWED_ROLES = (ROLE_SYSTEM, ROLE_USER, ROLE_TOOL_GUIDE)

# Target agent 분류
AGENT_MARKET_ANALYST = "market_analyst"
AGENT_RISK_EXPLAINER = "risk_explainer"
AGENT_PNL_EXPLAINER = "pnl_explainer"
AGENT_COMMON = "common"

ALLOWED_AGENTS = (
    AGENT_MARKET_ANALYST,
    AGENT_RISK_EXPLAINER,
    AGENT_PNL_EXPLAINER,
    AGENT_COMMON,
)

# 변수 자리표시자 패턴: {{ variable_name }}
VARIABLE_PATTERN = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")

# template_id 형식: "agent_name.template_name" (점 구분, alphanumeric/_)
TEMPLATE_ID_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*\.[a-zA-Z][a-zA-Z0-9_]*$")

# 버전 형식: "v1.0", "v2.3.1" 등
VERSION_PATTERN = re.compile(r"^v\d+(\.\d+){0,2}$")


# ─────────────────────────────────────────────────
# 데이터 모델 (Data Models)
# ─────────────────────────────────────────────────

@dataclass(frozen=True)
class PromptTemplate:
    """
    LLM-agnostic 프롬프트 템플릿.

    Fields:
        template_id: 고유 식별자 ("market_analyst.system" 등)
        version: 버전 ("v1.0")
        role: system/user/tool_guide
        body: 프롬프트 텍스트 (변수 자리표시자 포함)
        required_variables: 필수 변수 (자동 추출 + 명시)
        response_schema: JSON schema (응답 구조 강제 시 사용)
        target_agent: 대상 agent
        description: 사람-읽기용 설명
        source_path: 로드 출처 (디버깅용)
    """

    template_id: str
    version: str
    role: str
    body: str
    required_variables: tuple[str, ...]
    response_schema: Optional[dict[str, Any]]
    target_agent: str
    description: str = ""
    source_path: Optional[str] = None

    def __post_init__(self):
        # template_id 검증
        if not isinstance(self.template_id, str):
            raise ValueError(
                f"template_id must be str, got {type(self.template_id).__name__}"
            )
        if not TEMPLATE_ID_PATTERN.match(self.template_id):
            raise ValueError(
                f"template_id '{self.template_id}' invalid format — "
                f"expected 'agent_name.template_name'"
            )

        # version 검증
        if not VERSION_PATTERN.match(self.version):
            raise ValueError(
                f"version '{self.version}' invalid — expected 'vN' or 'vN.M'"
            )

        # role 검증
        if self.role not in ALLOWED_ROLES:
            raise ValueError(
                f"role '{self.role}' invalid — allowed: {ALLOWED_ROLES}"
            )

        # body 검증
        if not self.body or not isinstance(self.body, str):
            raise ValueError("body must be non-empty str")
        if len(self.body) > 100_000:  # 100KB 한도
            raise ValueError(
                f"body too large ({len(self.body)} chars > 100000)"
            )

        # target_agent 검증
        if self.target_agent not in ALLOWED_AGENTS:
            raise ValueError(
                f"target_agent '{self.target_agent}' invalid — "
                f"allowed: {ALLOWED_AGENTS}"
            )

        # required_variables 검증 (body의 자리표시자와 매칭)
        body_vars = set(extract_variables(self.body))
        declared_vars = set(self.required_variables)

        # body에 있지만 선언되지 않은 변수 → 거부
        undeclared = body_vars - declared_vars
        if undeclared:
            raise ValueError(
                f"body contains undeclared variables: {sorted(undeclared)} "
                f"— add to required_variables"
            )

        # 변수명 형식 검증
        for v in declared_vars:
            if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", v):
                raise ValueError(f"variable name '{v}' invalid")

        # response_schema 검증 (있으면 dict)
        if self.response_schema is not None:
            if not isinstance(self.response_schema, dict):
                raise ValueError(
                    f"response_schema must be dict or None, "
                    f"got {type(self.response_schema).__name__}"
                )
            if "type" not in self.response_schema:
                raise ValueError(
                    "response_schema must have 'type' key (JSON schema)"
                )

    # ─────────────────────────────────────────
    # 직렬화
    # ─────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "template_id": self.template_id,
            "version": self.version,
            "role": self.role,
            "body": self.body,
            "required_variables": list(self.required_variables),
            "response_schema": self.response_schema,
            "target_agent": self.target_agent,
            "description": self.description,
            "source_path": self.source_path,
        }

    def summary(self) -> dict[str, Any]:
        """body 제외 — 메타데이터만."""
        return {
            "template_id": self.template_id,
            "version": self.version,
            "role": self.role,
            "target_agent": self.target_agent,
            "required_variables": list(self.required_variables),
            "has_response_schema": self.response_schema is not None,
            "body_length": len(self.body),
            "description": self.description,
        }


@dataclass(frozen=True)
class RenderedPrompt:
    """렌더링 결과."""

    template_id: str
    version: str
    rendered_text: str
    variables_used: dict[str, str]   # 시크릿 마스킹 후
    rendered_at_utc: datetime
    response_schema: Optional[dict[str, Any]] = None

    def __post_init__(self):
        if self.rendered_at_utc.tzinfo is None:
            raise ValueError("rendered_at_utc must be tz-aware")

    def to_dict(self) -> dict[str, Any]:
        return {
            "template_id": self.template_id,
            "version": self.version,
            "rendered_text": self.rendered_text,
            "variables_used": dict(self.variables_used),
            "rendered_at_utc": self.rendered_at_utc.isoformat(),
            "response_schema": self.response_schema,
        }


# ─────────────────────────────────────────────────
# 변수 추출 (Variable Extraction)
# ─────────────────────────────────────────────────

def extract_variables(body: str) -> list[str]:
    """body 에서 {{ var_name }} 자리표시자 추출 — 정렬됨, 중복 제거."""
    if not body:
        return []
    found = VARIABLE_PATTERN.findall(body)
    return sorted(set(found))


__all__ = [
    "PromptTemplate",
    "RenderedPrompt",
    "extract_variables",
    "ROLE_SYSTEM",
    "ROLE_USER",
    "ROLE_TOOL_GUIDE",
    "ALLOWED_ROLES",
    "AGENT_MARKET_ANALYST",
    "AGENT_RISK_EXPLAINER",
    "AGENT_PNL_EXPLAINER",
    "AGENT_COMMON",
    "ALLOWED_AGENTS",
    "VARIABLE_PATTERN",
    "TEMPLATE_ID_PATTERN",
    "VERSION_PATTERN",
]
