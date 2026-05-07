"""
관측 인프라 (Observability Infrastructure)
============================================

JCPR Trading System - jcpr-ts-v01

Task A1 v0.1 — TraceContext (추적 컨텍스트)
Task A2 v0.1 — AuditWriter (통일 감사 로그)
Task A3 v0.1 — AuditIndexer (감사 로그 검색 + trace 재구성)

추적 가능한 운영 (Traceable Operations):
    모든 단계가 동일한 trace_id로 연결되어, 단일 ID로 전체 경로
    재구성 가능. MCP/Agent (Task 34-39) 도입 시 자동 통합.

사용 (Usage):
    from src.observability import (
        TraceContext, new_operator_trace,
        AuditWriter, configure_default_writer,
        AuditIndexer,
    )

    # 1. 앱 시작 시 1회: 기본 작성기 설정
    configure_default_writer("data/audit")

    # 2. 요청 시작: 추적 컨텍스트 생성
    ctx = new_operator_trace(
        operator_id="alice",
        session_id="session-2026-05-07",
    )

    # 3. 단계별 audit 기록
    writer = AuditWriter("data/audit")
    writer.write_signal(ctx, payload={"strategy": "momentum_v1"})

    # 4. 자식 span으로 하위 작업
    risk_ctx = ctx.child_span("risk_evaluation")
    writer.write_risk(risk_ctx, payload={"decision": "approve"})

    # 5. 사후 검색
    indexer = AuditIndexer("data/audit")
    events = indexer.find_by_trace(ctx.trace_id)
    tree = indexer.build_trace_tree(ctx.trace_id)
"""

from .audit_indexer import (
    AuditEvent,
    AuditIndexer,
    SpanNode,
    TraceSummary,
)
from .audit_writer import (
    ALLOWED_EVENT_TYPES,
    ALLOWED_SEVERITIES,
    SEVERITY_CRITICAL,
    SEVERITY_DEBUG,
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    AuditWriter,
    configure_default_writer,
    get_default_writer,
    reset_default_writer,
)
from .trace_context import (
    ALLOWED_ORIGINS,
    MASKED_VALUE,
    ORIGIN_AGENT,
    ORIGIN_BROKER_CALLBACK,
    ORIGIN_OPERATOR,
    ORIGIN_SCHEDULER,
    ORIGIN_SYSTEM,
    SECRET_KEYWORDS,
    TraceContext,
    generate_span_id,
    generate_trace_id,
    new_agent_trace,
    new_operator_trace,
    new_scheduler_trace,
)

__all__ = [
    # Task A1: TraceContext
    "TraceContext",
    "generate_trace_id",
    "generate_span_id",
    "new_operator_trace",
    "new_agent_trace",
    "new_scheduler_trace",
    "ORIGIN_OPERATOR",
    "ORIGIN_AGENT",
    "ORIGIN_SCHEDULER",
    "ORIGIN_BROKER_CALLBACK",
    "ORIGIN_SYSTEM",
    "ALLOWED_ORIGINS",
    "SECRET_KEYWORDS",
    "MASKED_VALUE",
    # Task A2: AuditWriter
    "AuditWriter",
    "configure_default_writer",
    "get_default_writer",
    "reset_default_writer",
    "ALLOWED_EVENT_TYPES",
    "ALLOWED_SEVERITIES",
    "SEVERITY_DEBUG",
    "SEVERITY_INFO",
    "SEVERITY_WARNING",
    "SEVERITY_ERROR",
    "SEVERITY_CRITICAL",
    # Task A3: AuditIndexer
    "AuditIndexer",
    "AuditEvent",
    "SpanNode",
    "TraceSummary",
]

__version__ = "0.1.0"
