"""
Agent Runner (공통 실행 루프)
==============================

JCPR Trading System - jcpr-ts-v01
Task 37 v0.1

Tasks 37/38/39 공통: 데이터 수집 → LLM 호출 → 응답 검증 → 결과 포장.
(Common loop: fetch data → invoke LLM → validate → wrap result.)

설계 (Design):
    - AgentRunner 클래스 — generic agent shell
    - 각 Task별 agent (37/38/39)는 다음만 정의:
        1. 어떤 system_template / user_task_template 을 쓸지
        2. 어떤 도구를 호출하고 데이터를 어떻게 LLM 입력으로 만들지
        3. fallback 응답을 어떻게 만들지 (LLM 실패 시)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from src.observability import (
    ORIGIN_AGENT,
    TraceContext,
    get_default_writer,
    new_agent_trace,
)

from ._llm_client import LLMClient, LLMRequest, LLMResponse
from ._mcp_client import MCPCallResult, MCPReadOnlyClient
from .prompts import (
    PromptRegistry,
    PromptTemplate,
    get_default_registry,
    safe_render,
    write_agent_decision,
    write_agent_prompt,
    write_agent_response,
)


logger = logging.getLogger("jcpr.agents.runner")


# ─────────────────────────────────────────────────
# 결과 (Result)
# ─────────────────────────────────────────────────

@dataclass(frozen=True)
class AgentRunResult:
    """Agent 실행 결과."""

    agent_name: str
    trace_id: str
    success: bool
    summary_ko: str
    response: Optional[dict[str, Any]]
    fallback_used: bool
    error: Optional[str]
    tool_calls_count: int
    llm_elapsed_ms: float
    total_elapsed_ms: float
    completed_at_utc: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_name": self.agent_name,
            "trace_id": self.trace_id,
            "success": self.success,
            "summary_ko": self.summary_ko,
            "response": self.response,
            "fallback_used": self.fallback_used,
            "error": self.error,
            "tool_calls_count": self.tool_calls_count,
            "llm_elapsed_ms": round(self.llm_elapsed_ms, 1),
            "total_elapsed_ms": round(self.total_elapsed_ms, 1),
            "completed_at_utc": self.completed_at_utc.isoformat(),
        }


# ─────────────────────────────────────────────────
# Agent context (도구 호출 결과 모음)
# ─────────────────────────────────────────────────

@dataclass
class AgentContext:
    """단일 agent 실행 동안 수집된 도구 호출 결과."""

    trace: TraceContext
    tool_results: list[MCPCallResult] = field(default_factory=list)

    def add(self, result: MCPCallResult) -> None:
        self.tool_results.append(result)

    def serialize_for_llm(self, *, max_size_bytes: int = 50_000) -> str:
        """
        LLM에 넘길 도구 결과 JSON 직렬화.

        Schema 검증된 JSON dict 들을 묶어 LLM에 전달.
        시크릿 마스킹은 mask_output에서 이미 처리됨.
        """
        payload = {
            "tool_calls": [
                {
                    "tool": r.tool_name,
                    "ok": r.success,
                    "data": _trim_for_llm(r.data),
                }
                for r in self.tool_results
            ]
        }
        text = json.dumps(payload, ensure_ascii=False, default=str)
        if len(text.encode("utf-8")) > max_size_bytes:
            # 크기 초과 — 잘라서 경고
            text = text[:max_size_bytes] + '..."truncated":true}'
        return text


def _trim_for_llm(data: Any, *, max_list_items: int = 20) -> Any:
    """LLM에 넘기기 전 큰 list 자르기."""
    if isinstance(data, list):
        if len(data) > max_list_items:
            return [
                _trim_for_llm(item, max_list_items=max_list_items)
                for item in data[:max_list_items]
            ] + [{"_truncated": f"+{len(data) - max_list_items} more"}]
        return [_trim_for_llm(item, max_list_items=max_list_items) for item in data]
    if isinstance(data, dict):
        return {k: _trim_for_llm(v, max_list_items=max_list_items)
                for k, v in data.items()}
    return data


# ─────────────────────────────────────────────────
# AgentSpec (각 Task가 정의)
# ─────────────────────────────────────────────────

# 도구 호출 순서 정의 callable 시그니처
ToolCollectorFn = Callable[
    ["AgentContext", MCPReadOnlyClient, dict[str, Any]],
    None,
]

# Fallback 생성 callable 시그니처
FallbackFn = Callable[
    ["AgentContext", dict[str, Any]],
    dict[str, Any],
]


@dataclass
class AgentSpec:
    """
    Agent 사양 — 각 Task별 (37/38/39) 인스턴스 생성.

    Args:
        agent_name: 식별자 (e.g. "market_analyst")
        system_template_id: Task 36 system prompt id
        user_template_id: Task 36 user task template id
        tool_collector: 도구 호출 함수 (Task별 다름)
        fallback_builder: LLM 실패 시 응답 생성
        max_tool_calls: 최대 도구 호출 수 (보호 한도)
    """

    agent_name: str
    system_template_id: str
    user_template_id: str
    tool_collector: ToolCollectorFn
    fallback_builder: FallbackFn
    max_tool_calls: int = 10


# ─────────────────────────────────────────────────
# AgentRunner (메인)
# ─────────────────────────────────────────────────

@dataclass
class AgentRunner:
    """
    공통 실행 루프.

    Args:
        spec: AgentSpec (Task별 정의)
        llm_client: LLMClient (Mock 또는 실제)
        mcp_client: MCPReadOnlyClient
        prompt_registry: PromptRegistry (None이면 default)
        operator_id: 운영자 ID (audit 기록용)
        session_id: 세션 ID
    """

    spec: AgentSpec
    llm_client: LLMClient
    mcp_client: MCPReadOnlyClient
    prompt_registry: Optional[PromptRegistry] = None
    operator_id: str = "operator-default"
    session_id: str = "session-default"

    def __post_init__(self):
        if self.prompt_registry is None:
            self.prompt_registry = get_default_registry()

    # ─────────────────────────────────────────
    # 메인 실행
    # ─────────────────────────────────────────

    def run(
        self,
        *,
        system_variables: dict[str, Any],
        user_variables: dict[str, Any],
        operator_query: str = "",
    ) -> AgentRunResult:
        """
        Agent 실행.

        Args:
            system_variables: system_prompt에 치환할 변수
                (e.g. {"session_id": "...", "operator_id": "..."})
            user_variables: user_task_prompt에 치환할 변수
                (Task별 — 예: {"starting_capital_krw": "...", "cash_krw": "..."})
            operator_query: 운영자가 입력한 자연어 (선택, audit 기록)

        Returns:
            AgentRunResult
        """
        # 1. Trace 생성
        ctx = new_agent_trace(
            agent_name=self.spec.agent_name,
            session_id=self.session_id,
            correlation_keys={
                "operator_id": self.operator_id,
                "system_template": self.spec.system_template_id,
                "user_template": self.spec.user_template_id,
            },
        )
        # MCP client에 parent trace 주입
        self.mcp_client.parent_trace = ctx

        start_total = datetime.now(timezone.utc)

        try:
            return self._run_impl(
                ctx=ctx,
                system_variables=system_variables,
                user_variables=user_variables,
                operator_query=operator_query,
                start_total=start_total,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception(f"agent run failed: {self.spec.agent_name}")
            writer = get_default_writer()
            if writer:
                writer.write_exception(ctx, e, additional={
                    "agent": self.spec.agent_name,
                })
            return AgentRunResult(
                agent_name=self.spec.agent_name,
                trace_id=ctx.trace_id,
                success=False,
                summary_ko=f"{self.spec.agent_name} 실행 중 오류: {type(e).__name__}",
                response=None,
                fallback_used=False,
                error=f"{type(e).__name__}: {e}",
                tool_calls_count=0,
                llm_elapsed_ms=0.0,
                total_elapsed_ms=(
                    datetime.now(timezone.utc) - start_total
                ).total_seconds() * 1000.0,
                completed_at_utc=datetime.now(timezone.utc),
            )

    # ─────────────────────────────────────────
    # 내부 구현
    # ─────────────────────────────────────────

    def _run_impl(
        self,
        *,
        ctx: TraceContext,
        system_variables: dict[str, Any],
        user_variables: dict[str, Any],
        operator_query: str,
        start_total: datetime,
    ) -> AgentRunResult:
        # 2. 프롬프트 로드 + 렌더링
        sys_tmpl = self.prompt_registry.get(self.spec.system_template_id)
        usr_tmpl = self.prompt_registry.get(self.spec.user_template_id)

        try:
            sys_rendered = safe_render(sys_tmpl, system_variables)
            usr_rendered = safe_render(usr_tmpl, user_variables)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"prompt render failed: {e}") from e

        # 3. 도구 호출 (Task별 collector)
        agent_ctx = AgentContext(trace=ctx)
        try:
            self.spec.tool_collector(agent_ctx, self.mcp_client, user_variables)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"tool_collector failed: {e}")
            # 도구 호출 부분 실패해도 LLM에 전달 시도

        # 보호 한도
        if len(agent_ctx.tool_results) > self.spec.max_tool_calls:
            logger.warning(
                f"agent {self.spec.agent_name} exceeded max_tool_calls "
                f"({len(agent_ctx.tool_results)} > {self.spec.max_tool_calls})"
            )

        # 4. LLM에 전달할 user_prompt 보강
        tool_data_block = agent_ctx.serialize_for_llm()
        full_user_prompt = (
            f"{usr_rendered.rendered_text}\n\n"
            f"---\n\n"
            f"# Tool Call Results\n\n"
            f"```json\n{tool_data_block}\n```\n\n"
            f"---\n\n"
            f"Operator query: {operator_query or '(none)'}\n\n"
            f"Now respond per the response schema in Korean."
        )

        # 5. Audit prompt
        write_agent_prompt(ctx.child_span("prompt"), sys_rendered)
        write_agent_prompt(ctx.child_span("prompt"), usr_rendered)

        # 6. LLM 호출
        request = LLMRequest(
            system_prompt=sys_rendered.rendered_text,
            user_prompt=full_user_prompt,
            response_schema=sys_rendered.response_schema or usr_rendered.response_schema,
            metadata={"agent": self.spec.agent_name},
        )

        llm_start = datetime.now(timezone.utc)
        try:
            llm_response = self.llm_client.invoke(request)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"LLM invoke failed: {e}")
            llm_response = LLMResponse(
                raw_text="",
                parsed_json=None,
                parse_error=f"{type(e).__name__}: {e}",
                schema_validated=False,
                schema_error=None,
                model_id=self.llm_client.model_id,
                elapsed_ms=0.0,
                received_at_utc=datetime.now(timezone.utc),
            )
        llm_elapsed_ms = (
            datetime.now(timezone.utc) - llm_start
        ).total_seconds() * 1000.0

        # 7. Audit response
        write_agent_response(
            ctx.child_span("response"),
            template_id=self.spec.user_template_id,
            response_text=llm_response.raw_text,
            response_parsed=llm_response.parsed_json,
            schema_validated=llm_response.schema_validated,
        )

        # 8. 결과 결정 (성공 / fallback)
        fallback_used = False
        if llm_response.is_success:
            response_dict = llm_response.parsed_json
            success = True
            error = None
        else:
            # Fallback
            try:
                response_dict = self.spec.fallback_builder(agent_ctx, user_variables)
                fallback_used = True
                success = True  # fallback도 운영자에게는 성공 (정보 전달)
                error = (
                    f"LLM failed (parse: {llm_response.parse_error}, "
                    f"schema: {llm_response.schema_error}) — fallback used"
                )
            except Exception as e:  # noqa: BLE001
                logger.exception("fallback_builder failed")
                response_dict = None
                success = False
                error = (
                    f"LLM failed AND fallback failed: "
                    f"{type(e).__name__}: {e}"
                )

        # summary 추출
        summary_ko = ""
        if response_dict and isinstance(response_dict, dict):
            summary_ko = str(response_dict.get("summary_ko", ""))[:1500]

        # 9. Decision audit
        write_agent_decision(
            ctx.child_span("decision"),
            decision=summary_ko,
            template_id=self.spec.user_template_id,
            additional={
                "fallback_used": fallback_used,
                "tool_calls_count": len(agent_ctx.tool_results),
            },
        )

        total_elapsed_ms = (
            datetime.now(timezone.utc) - start_total
        ).total_seconds() * 1000.0

        return AgentRunResult(
            agent_name=self.spec.agent_name,
            trace_id=ctx.trace_id,
            success=success,
            summary_ko=summary_ko,
            response=response_dict,
            fallback_used=fallback_used,
            error=error,
            tool_calls_count=len(agent_ctx.tool_results),
            llm_elapsed_ms=llm_elapsed_ms,
            total_elapsed_ms=total_elapsed_ms,
            completed_at_utc=datetime.now(timezone.utc),
        )


__all__ = [
    "AgentRunResult",
    "AgentContext",
    "AgentSpec",
    "AgentRunner",
    "ToolCollectorFn",
    "FallbackFn",
]
