"""
추적 컨텍스트 (Trace Context)
==============================

JCPR Trading System - jcpr-ts-v01
Task A1 v0.1 — Observability Infrastructure

단일 요청의 전체 경로를 식별하는 추적 컨텍스트.
(Trace context that identifies the full path of a single request.)

설계 (Design):
    - W3C Trace Context / OpenTelemetry 호환 패턴
    - 단일 trace_id가 여러 span_id를 묶음 (계층 구조)
    - 모든 audit log에 자동 첨부 (Task A2 AuditWriter가 사용)
    - 시크릿 자동 마스킹 (correlation_keys 검사)

사용 (Usage):
    # 자동 생성 (권장)
    ctx = TraceContext.new(
        origin="operator",
        operator_id="alice",
        session_id="session-2026-05-07",
    )

    # 외부 주입 (특수 케이스)
    ctx = TraceContext.new(
        trace_id="trc-20260507-custom01",
        origin="broker_callback",
        session_id="session-2026-05-07",
    )

    # 자식 span (현재 trace 유지, 새 작업 단위)
    child_ctx = ctx.child_span("risk_evaluation")

    # 감사 로그용 dict (시크릿 마스킹 자동)
    audit_dict = ctx.to_audit_dict()
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


# ─────────────────────────────────────────────────
# 상수 (Constants)
# ─────────────────────────────────────────────────

# trace_id 형식: trc-YYYYMMDD-{8자리 hex}
TRACE_ID_PATTERN = re.compile(r"^trc-\d{8}-[a-f0-9]{8,16}$")
SPAN_ID_PATTERN = re.compile(r"^spn-[a-f0-9]{8,16}$")

# Origin 분류 (5종)
ORIGIN_OPERATOR = "operator"           # 사람 직접 입력
ORIGIN_AGENT = "agent"                 # LLM agent 결정
ORIGIN_SCHEDULER = "scheduler"         # 자동 스케줄러
ORIGIN_BROKER_CALLBACK = "broker_callback"  # 브로커 콜백
ORIGIN_SYSTEM = "system"               # 내부 시스템 작업

ALLOWED_ORIGINS = (
    ORIGIN_OPERATOR,
    ORIGIN_AGENT,
    ORIGIN_SCHEDULER,
    ORIGIN_BROKER_CALLBACK,
    ORIGIN_SYSTEM,
)

# 시크릿 키워드 — correlation_keys에서 검사
SECRET_KEYWORDS = (
    "api_key", "apikey", "api-key",
    "secret", "secret_key",
    "password", "passwd",
    "token", "bearer",
    "private_key", "privatekey",
    "auth", "authorization",
    "credential",
)

# 마스킹된 값 표시
MASKED_VALUE = "***MASKED***"


# ─────────────────────────────────────────────────
# 검증 헬퍼 (Validation Helpers)
# ─────────────────────────────────────────────────

def _check_no_secret_keys(d: dict[str, Any], path: str = "") -> None:
    """correlation_keys에 시크릿 키워드 발견 시 ValueError."""
    for k, v in d.items():
        full = f"{path}.{k}" if path else str(k)
        low = str(k).lower()
        for kw in SECRET_KEYWORDS:
            if kw in low:
                raise ValueError(
                    f"correlation_keys '{full}'에 시크릿 키워드 '{kw}' 포함 — "
                    f"환경변수로만 전달 가능"
                )
        # 값도 검사 (문자열인 경우)
        if isinstance(v, str) and len(v) >= 32:
            # 긴 base64-like 문자열은 의심
            if re.match(r"^[A-Za-z0-9+/=_\-]+$", v):
                raise ValueError(
                    f"correlation_keys 값 '{full}'이 자격증명처럼 보임 "
                    f"(긴 영숫자 — {len(v)}자)"
                )


def _validate_trace_id(trace_id: str) -> str:
    """trace_id 형식 검증."""
    if not isinstance(trace_id, str):
        raise ValueError(f"trace_id must be str, got {type(trace_id).__name__}")
    if not TRACE_ID_PATTERN.match(trace_id):
        raise ValueError(
            f"trace_id '{trace_id}' 형식 오류 — "
            f"'trc-YYYYMMDD-XXXXXXXX' 형식 (X는 hex)"
        )
    return trace_id


def _validate_span_id(span_id: str) -> str:
    """span_id 형식 검증."""
    if not isinstance(span_id, str):
        raise ValueError(f"span_id must be str, got {type(span_id).__name__}")
    if not SPAN_ID_PATTERN.match(span_id):
        raise ValueError(
            f"span_id '{span_id}' 형식 오류 — 'spn-XXXXXXXX' 형식"
        )
    return span_id


# ─────────────────────────────────────────────────
# ID 생성 (ID Generators)
# ─────────────────────────────────────────────────

def generate_trace_id(*, now_utc: Optional[datetime] = None) -> str:
    """
    새 trace_id 생성 — UUID4 기반.

    Returns:
        "trc-YYYYMMDD-{8자리 hex}"
    """
    now = now_utc or datetime.now(timezone.utc)
    date_str = now.strftime("%Y%m%d")
    short = uuid.uuid4().hex[:8]
    return f"trc-{date_str}-{short}"


def generate_span_id() -> str:
    """
    새 span_id 생성 — UUID4 기반.

    Returns:
        "spn-{8자리 hex}"
    """
    short = uuid.uuid4().hex[:8]
    return f"spn-{short}"


# ─────────────────────────────────────────────────
# TraceContext (메인 데이터 모델)
# ─────────────────────────────────────────────────

@dataclass(frozen=True)
class TraceContext:
    """
    단일 요청 전 경로를 식별하는 추적 컨텍스트.

    Fields:
        trace_id: 전체 요청 식별 (UUID4 기반)
        span_id: 현재 작업 단위 식별
        parent_span_id: 부모 span (계층 구조 — None이면 루트)
        origin: 발원 (operator/agent/scheduler/broker_callback/system)
        operator_id: 발원자 ID (사람 username, agent name 등)
        session_id: 운영 세션 (Task 49의 session_id와 동일)
        correlation_keys: 추가 컨텍스트 (symbol, strategy_id, order_id 등)
        started_at_utc: 시작 시각 (UTC tz-aware)
    """

    trace_id: str
    span_id: str
    parent_span_id: Optional[str]
    origin: str
    operator_id: Optional[str]
    session_id: str
    correlation_keys: dict[str, Any]
    started_at_utc: datetime

    # ─────────────────────────────────────────
    # 검증 (post-init)
    # ─────────────────────────────────────────

    def __post_init__(self):
        # trace_id / span_id 형식
        _validate_trace_id(self.trace_id)
        _validate_span_id(self.span_id)
        if self.parent_span_id is not None:
            _validate_span_id(self.parent_span_id)

        # origin 화이트리스트
        if self.origin not in ALLOWED_ORIGINS:
            raise ValueError(
                f"origin '{self.origin}' invalid — "
                f"allowed: {ALLOWED_ORIGINS}"
            )

        # session_id 비어있으면 안 됨
        if not self.session_id or not isinstance(self.session_id, str):
            raise ValueError("session_id must be non-empty string")

        # tz-aware 강제
        if self.started_at_utc.tzinfo is None:
            raise ValueError("started_at_utc must be tz-aware UTC datetime")

        # correlation_keys 시크릿 검사
        if self.correlation_keys:
            _check_no_secret_keys(self.correlation_keys)

    # ─────────────────────────────────────────
    # 팩토리 (Factory)
    # ─────────────────────────────────────────

    @classmethod
    def new(
        cls,
        *,
        origin: str,
        session_id: str,
        operator_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        span_id: Optional[str] = None,
        parent_span_id: Optional[str] = None,
        correlation_keys: Optional[dict[str, Any]] = None,
        started_at_utc: Optional[datetime] = None,
    ) -> "TraceContext":
        """
        새 TraceContext 생성 — 자동 또는 주입.

        Args:
            origin: 발원 (필수)
            session_id: 운영 세션 (필수)
            operator_id: 발원자
            trace_id: 외부 주입 시 명시 (None이면 자동 생성)
            span_id: 외부 주입 시 명시 (None이면 자동 생성)
            parent_span_id: 부모 span
            correlation_keys: 추가 컨텍스트
            started_at_utc: 시작 시각 (None이면 now)

        Returns:
            TraceContext (frozen)
        """
        now = started_at_utc or datetime.now(timezone.utc)
        return cls(
            trace_id=trace_id or generate_trace_id(now_utc=now),
            span_id=span_id or generate_span_id(),
            parent_span_id=parent_span_id,
            origin=origin,
            operator_id=operator_id,
            session_id=session_id,
            correlation_keys=correlation_keys or {},
            started_at_utc=now,
        )

    # ─────────────────────────────────────────
    # 자식 span 생성 (Hierarchical)
    # ─────────────────────────────────────────

    def child_span(
        self,
        span_name: str,
        *,
        additional_correlation: Optional[dict[str, Any]] = None,
    ) -> "TraceContext":
        """
        자식 span 생성 — 같은 trace_id 유지, 새 span_id 생성.

        Args:
            span_name: 작업 이름 (correlation_keys에 'span_name' 키로 추가)
            additional_correlation: 추가 컨텍스트 (병합)

        Returns:
            새 TraceContext (parent_span_id = self.span_id)
        """
        # 기존 + 추가 + span_name
        new_keys = dict(self.correlation_keys)
        if additional_correlation:
            new_keys.update(additional_correlation)
        new_keys["span_name"] = span_name

        return TraceContext(
            trace_id=self.trace_id,
            span_id=generate_span_id(),
            parent_span_id=self.span_id,
            origin=self.origin,
            operator_id=self.operator_id,
            session_id=self.session_id,
            correlation_keys=new_keys,
            started_at_utc=datetime.now(timezone.utc),
        )

    # ─────────────────────────────────────────
    # 직렬화 (Serialization)
    # ─────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """전체 dict — JSON 직렬화 가능."""
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "origin": self.origin,
            "operator_id": self.operator_id,
            "session_id": self.session_id,
            "correlation_keys": dict(self.correlation_keys),
            "started_at_utc": self.started_at_utc.isoformat(),
        }

    def to_audit_dict(self) -> dict[str, Any]:
        """
        감사 로그용 dict — 시크릿 자동 마스킹.

        post-init 검증으로 시크릿이 들어오는 것은 차단되지만,
        defense-in-depth로 출력 시 한 번 더 마스킹 검사.
        """
        d = self.to_dict()
        # correlation_keys 한 번 더 마스킹
        masked_keys: dict[str, Any] = {}
        for k, v in d["correlation_keys"].items():
            low = str(k).lower()
            if any(kw in low for kw in SECRET_KEYWORDS):
                masked_keys[k] = MASKED_VALUE
            else:
                masked_keys[k] = v
        d["correlation_keys"] = masked_keys
        return d

    # ─────────────────────────────────────────
    # 표시 (Display) — 시크릿 안전
    # ─────────────────────────────────────────

    def __repr__(self) -> str:
        # correlation_keys 노출 안 함 (시크릿 가능성)
        return (
            f"TraceContext(trace_id={self.trace_id!r}, "
            f"span_id={self.span_id!r}, "
            f"origin={self.origin!r}, "
            f"session_id={self.session_id!r})"
        )

    def short_id(self) -> str:
        """짧은 표시용 — 'trc-...d4 / spn-...b8'."""
        t_short = self.trace_id[-8:]
        s_short = self.span_id[-8:]
        return f"trc-...{t_short} / spn-...{s_short}"


# ─────────────────────────────────────────────────
# 편의 함수 (Convenience)
# ─────────────────────────────────────────────────

def new_operator_trace(
    operator_id: str,
    session_id: str,
    *,
    correlation_keys: Optional[dict[str, Any]] = None,
) -> TraceContext:
    """운영자 발원 trace 생성 — 가장 흔한 케이스."""
    return TraceContext.new(
        origin=ORIGIN_OPERATOR,
        operator_id=operator_id,
        session_id=session_id,
        correlation_keys=correlation_keys,
    )


def new_agent_trace(
    agent_name: str,
    session_id: str,
    *,
    correlation_keys: Optional[dict[str, Any]] = None,
) -> TraceContext:
    """Agent 발원 trace 생성."""
    return TraceContext.new(
        origin=ORIGIN_AGENT,
        operator_id=agent_name,
        session_id=session_id,
        correlation_keys=correlation_keys,
    )


def new_scheduler_trace(
    job_name: str,
    session_id: str,
    *,
    correlation_keys: Optional[dict[str, Any]] = None,
) -> TraceContext:
    """스케줄러 발원 trace 생성."""
    return TraceContext.new(
        origin=ORIGIN_SCHEDULER,
        operator_id=job_name,
        session_id=session_id,
        correlation_keys=correlation_keys,
    )
