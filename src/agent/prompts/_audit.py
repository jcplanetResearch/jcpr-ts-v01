"""
프롬프트 audit 헬퍼 (Prompt Audit Helpers)
=============================================

JCPR Trading System - jcpr-ts-v01
Task 36 v0.1

프롬프트 사용을 Task A2 AuditWriter로 자동 기록.
(Auto-records prompt usage via Task A2.)
"""

from __future__ import annotations

from typing import Any, Optional

from src.observability import (
    AuditWriter,
    TraceContext,
    get_default_writer,
)

from ._template import PromptTemplate, RenderedPrompt


def write_agent_prompt(
    ctx: TraceContext,
    rendered: RenderedPrompt,
    *,
    writer: Optional[AuditWriter] = None,
) -> bool:
    """
    렌더링된 프롬프트를 audit 기록.

    Args:
        ctx: TraceContext
        rendered: RenderedPrompt
        writer: 명시 시 사용 (None이면 default writer)

    Returns:
        True if recorded, False if no writer
    """
    w = writer or get_default_writer()
    if w is None:
        return False
    payload = {
        "template_id": rendered.template_id,
        "version": rendered.version,
        "rendered_text_length": len(rendered.rendered_text),
        "variables_used": rendered.variables_used,  # 자동 마스킹됨
        "has_response_schema": rendered.response_schema is not None,
    }
    return w.write(
        event_type="agent_prompt",
        ctx=ctx,
        payload=payload,
    )


def write_agent_response(
    ctx: TraceContext,
    *,
    template_id: str,
    response_text: str,
    response_parsed: Optional[dict] = None,
    schema_validated: Optional[bool] = None,
    writer: Optional[AuditWriter] = None,
) -> bool:
    """
    Agent 응답 audit.

    Args:
        ctx: TraceContext (보통 prompt와 같은 trace, 자식 span)
        template_id: 사용된 템플릿
        response_text: LLM 응답 텍스트
        response_parsed: schema 임범 시 파싱된 dict
        schema_validated: schema 검증 결과
    """
    w = writer or get_default_writer()
    if w is None:
        return False
    payload: dict[str, Any] = {
        "template_id": template_id,
        "response_text_length": len(response_text),
    }
    if response_parsed is not None:
        # 파싱 결과 키만 (큰 값 차단)
        payload["response_keys"] = list(response_parsed.keys())[:30]
    if schema_validated is not None:
        payload["schema_validated"] = schema_validated
    return w.write(
        event_type="agent_response",
        ctx=ctx,
        payload=payload,
    )


def write_agent_decision(
    ctx: TraceContext,
    *,
    decision: str,
    reason: str = "",
    template_id: Optional[str] = None,
    additional: Optional[dict] = None,
    writer: Optional[AuditWriter] = None,
) -> bool:
    """
    Agent의 결정 audit (예: "buy 005930 10주", "halt trading" 등).

    Args:
        ctx: TraceContext
        decision: 결정 요약 (≤200 chars)
        reason: 사유
        template_id: 사용된 프롬프트
        additional: 추가 메타데이터
    """
    w = writer or get_default_writer()
    if w is None:
        return False
    payload: dict[str, Any] = {
        "decision": decision[:200] if isinstance(decision, str) else str(decision)[:200],
        "reason": reason[:500] if isinstance(reason, str) else str(reason)[:500],
    }
    if template_id:
        payload["template_id"] = template_id
    if additional:
        # 자동 마스킹 (Task A2 _mask_payload)
        payload.update(additional)
    return w.write(
        event_type="agent_decision",
        ctx=ctx,
        payload=payload,
    )


__all__ = [
    "write_agent_prompt",
    "write_agent_response",
    "write_agent_decision",
]
