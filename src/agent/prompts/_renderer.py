"""
프롬프트 렌더러 (Prompt Renderer)
==================================

JCPR Trading System - jcpr-ts-v01
Task 36 v0.1

PromptTemplate에 변수를 안전하게 치환.
(Safely substitutes variables into PromptTemplate.)

보안 (Security):
    1. 변수 키에 시크릿 키워드 자동 차단 (Task A1 SECRET_KEYWORDS 재사용)
    2. 변수 값의 PII 자동 차단 (operator_id 등)
    3. 자리표시자 누락 시 에러 (안전한 실패)
    4. 추가 변수 (자리표시자 없는데 전달됨) 경고
    5. 값 길이 제한 (단일 변수 ≤ 50KB, 전체 ≤ 200KB)
    6. 값 타입 강제 — str/int/float/Decimal/bool만 (dict/list 금지 — 별도 직렬화)
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from src.observability.trace_context import (
    MASKED_VALUE,
    SECRET_KEYWORDS,
)

from ._template import (
    PromptTemplate,
    RenderedPrompt,
    VARIABLE_PATTERN,
    extract_variables,
)


# ─────────────────────────────────────────────────
# 한도 (Limits)
# ─────────────────────────────────────────────────

MAX_SINGLE_VALUE_BYTES = 50_000     # 단일 변수 50KB
MAX_TOTAL_RENDERED_BYTES = 200_000  # 렌더링 결과 200KB
MAX_VARIABLE_COUNT = 100             # 변수 100개 한도

# PII 키워드 (Task 34 _security.py 와 동일)
PII_KEYS = (
    "operator_id_full",
    "account_number",
    "account_id_full",
    "phone",
    "email",
    "ssn",
    "personal_name",
    "ip_address",
    "user_agent",
)


# ─────────────────────────────────────────────────
# 예외 (Exceptions)
# ─────────────────────────────────────────────────

class RenderError(Exception):
    """렌더링 오류 — 보안/검증/누락 등."""


class SecretInVariableError(RenderError):
    """변수에 시크릿 포함."""


class VariableMissingError(RenderError):
    """필수 변수 누락."""


class ValueTooLargeError(RenderError):
    """값 크기 초과."""


# ─────────────────────────────────────────────────
# 값 검증 + 정규화 (Value Validation)
# ─────────────────────────────────────────────────

def _check_secret_in_key(key: str) -> None:
    """변수 키에 시크릿 키워드 → 거부."""
    low = key.lower()
    for kw in SECRET_KEYWORDS:
        if kw in low:
            raise SecretInVariableError(
                f"variable key '{key}' contains secret keyword '{kw}' — "
                f"never put credentials in prompt variables"
            )


def _check_pii_in_key(key: str) -> bool:
    """변수 키가 PII이면 True (마스킹 대상)."""
    low = key.lower()
    return any(pii in low for pii in PII_KEYS)


def _normalize_value(key: str, value: Any) -> str:
    """
    변수 값 → str.

    허용 타입: str, int, float, Decimal, bool
    금지: dict, list (구조화 데이터는 호출자가 JSON 직렬화 후 str 전달)
    """
    if value is None:
        return "(none)"

    if isinstance(value, bool):
        return "true" if value else "false"

    if isinstance(value, (int, float)):
        return str(value)

    if isinstance(value, Decimal):
        return str(value)

    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_SINGLE_VALUE_BYTES:
            raise ValueTooLargeError(
                f"variable '{key}' value exceeds {MAX_SINGLE_VALUE_BYTES:,} bytes"
            )
        return value

    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()

    # 구조화 데이터 거부
    if isinstance(value, (dict, list, tuple, set)):
        raise RenderError(
            f"variable '{key}' has unsupported type {type(value).__name__} — "
            f"serialize to str (JSON) before passing to render()"
        )

    # 알 수 없는 타입
    raise RenderError(
        f"variable '{key}' has unsupported type {type(value).__name__}"
    )


# ─────────────────────────────────────────────────
# 메인 렌더링 함수 (Main Render)
# ─────────────────────────────────────────────────

def safe_render(
    template: PromptTemplate,
    variables: dict[str, Any],
    *,
    allow_extra_variables: bool = False,
    rendered_at_utc: datetime | None = None,
) -> RenderedPrompt:
    """
    안전한 변수 치환.

    Args:
        template: PromptTemplate
        variables: {var_name: value} dict
        allow_extra_variables: 자리표시자 없는데 전달된 변수 허용
        rendered_at_utc: 명시 시 사용 (None이면 now)

    Returns:
        RenderedPrompt

    Raises:
        SecretInVariableError: 시크릿 키워드 포함 변수
        VariableMissingError: 필수 변수 누락
        ValueTooLargeError: 값 크기 초과
        RenderError: 기타
    """
    if not isinstance(template, PromptTemplate):
        raise RenderError(
            f"template must be PromptTemplate, got {type(template).__name__}"
        )

    if not isinstance(variables, dict):
        raise RenderError(
            f"variables must be dict, got {type(variables).__name__}"
        )

    if len(variables) > MAX_VARIABLE_COUNT:
        raise RenderError(
            f"too many variables: {len(variables)} > max {MAX_VARIABLE_COUNT}"
        )

    # 1. 시크릿 키 사전 차단 (모든 키)
    for k in variables:
        if not isinstance(k, str):
            raise RenderError(f"variable key must be str, got {type(k).__name__}")
        _check_secret_in_key(k)

    # 2. 필수 변수 검증
    declared = set(template.required_variables)
    body_vars = set(extract_variables(template.body))
    needed = declared | body_vars
    provided = set(variables.keys())

    missing = needed - provided
    if missing:
        raise VariableMissingError(
            f"missing required variables: {sorted(missing)}"
        )

    extra = provided - needed
    if extra and not allow_extra_variables:
        raise RenderError(
            f"unexpected variables: {sorted(extra)} "
            f"(use allow_extra_variables=True to permit)"
        )

    # 3. 값 정규화 + 마스킹 dict 작성
    masked_used: dict[str, str] = {}
    rendered_values: dict[str, str] = {}

    for k, v in variables.items():
        rendered_values[k] = _normalize_value(k, v)
        # 마스킹 — PII 키이면 마스킹
        if _check_pii_in_key(k):
            masked_used[k] = MASKED_VALUE
        else:
            # 값 자체에 시크릿 키워드 의심 (긴 base64-like) → 마스킹
            val = rendered_values[k]
            if _looks_like_credential(val):
                masked_used[k] = MASKED_VALUE
                # 렌더링 값도 마스킹 (LLM에 노출 금지)
                rendered_values[k] = MASKED_VALUE
            else:
                masked_used[k] = val

    # 4. 자리표시자 치환
    def _replace(m):
        var_name = m.group(1)
        return rendered_values.get(var_name, m.group(0))

    rendered_text = VARIABLE_PATTERN.sub(_replace, template.body)

    # 5. 결과 크기 검증
    if len(rendered_text.encode("utf-8")) > MAX_TOTAL_RENDERED_BYTES:
        raise ValueTooLargeError(
            f"rendered text exceeds {MAX_TOTAL_RENDERED_BYTES:,} bytes"
        )

    # 6. 잔여 자리표시자 검증 (추가 검증)
    remaining = VARIABLE_PATTERN.findall(rendered_text)
    if remaining:
        raise RenderError(
            f"unrendered placeholders remain: {sorted(set(remaining))}"
        )

    return RenderedPrompt(
        template_id=template.template_id,
        version=template.version,
        rendered_text=rendered_text,
        variables_used=masked_used,
        rendered_at_utc=rendered_at_utc or datetime.now(timezone.utc),
        response_schema=template.response_schema,
    )


def _looks_like_credential(value: str) -> bool:
    """값이 자격증명처럼 보이는지 — 긴 base64-like."""
    import re
    if len(value) < 32:
        return False
    # 영숫자 + base64 문자만 + 공백 없음 → 의심
    return bool(re.match(r"^[A-Za-z0-9+/=_\-]{32,}$", value))


__all__ = [
    "safe_render",
    "RenderError",
    "SecretInVariableError",
    "VariableMissingError",
    "ValueTooLargeError",
    "MAX_SINGLE_VALUE_BYTES",
    "MAX_TOTAL_RENDERED_BYTES",
    "MAX_VARIABLE_COUNT",
]
