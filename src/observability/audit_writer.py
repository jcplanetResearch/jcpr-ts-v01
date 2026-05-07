"""
감사 로그 작성기 (Audit Writer)
=================================

JCPR Trading System - jcpr-ts-v01
Task A2 v0.1 — Observability Infrastructure

통일된 JSONL 감사 로그 작성기. 모든 audit 이벤트는 TraceContext를 첨부.
(Unified JSONL audit log writer. All events attach TraceContext.)

설계 (Design):
    - JSONL 형식 (한 줄 = 한 이벤트)
    - 자동 timestamp + trace_id 첨부
    - 시크릿 자동 마스킹 (defense-in-depth)
    - 파일 회전 (날짜별)
    - 동시 쓰기 안전 (file lock)
    - 실패 시 fallback (stderr)

이벤트 카테고리 (Event Categories):
    - signal_generated   : 시그널 생성 (Task 14, 16)
    - risk_evaluation    : 리스크 게이트 평가 (Task 19)
    - order_intent       : 주문 의도 생성 (Task 17)
    - order_submitted    : 주문 전송 (Task 21)
    - fill_received      : 체결 수신 (Task 24)
    - approval_request   : 승인 요청 (Task 40)
    - approval_decision  : 승인 결정 (Task 40)
    - mcp_tool_call      : MCP 도구 호출 (Task 34, 35)
    - agent_decision     : Agent 의사결정 (Task 37-39)
    - reconciliation     : 정합성 점검 (Task 28)
    - exception          : 예외 발생
    - capacity_decision  : Capacity 추천 (Task 49)
    - system_event       : 시스템 이벤트 (시작/종료/kill switch)

사용 (Usage):
    writer = AuditWriter(audit_dir="data/audit")
    writer.write(
        event_type="risk_evaluation",
        ctx=trace_ctx,
        payload={"decision": "approve", "symbol": "005930"},
    )
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from .trace_context import (
    MASKED_VALUE,
    SECRET_KEYWORDS,
    TraceContext,
)


# ─────────────────────────────────────────────────
# 상수 (Constants)
# ─────────────────────────────────────────────────

# 표준 이벤트 타입 (확장 가능)
ALLOWED_EVENT_TYPES = frozenset({
    # Trading flow
    "signal_generated",
    "risk_evaluation",
    "order_intent",
    "order_submitted",
    "order_filled",
    "fill_received",
    "order_cancelled",
    # Approval
    "approval_request",
    "approval_decision",
    # MCP / Agent
    "mcp_tool_call",
    "mcp_tool_result",
    "agent_decision",
    "agent_prompt",
    "agent_response",
    # System
    "reconciliation",
    "exception",
    "capacity_decision",
    "system_event",
    "session_start",
    "session_end",
    # Catch-all
    "other",
})

# 표준 severity
SEVERITY_DEBUG = "debug"
SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_ERROR = "error"
SEVERITY_CRITICAL = "critical"

ALLOWED_SEVERITIES = (
    SEVERITY_DEBUG, SEVERITY_INFO, SEVERITY_WARNING,
    SEVERITY_ERROR, SEVERITY_CRITICAL,
)


# ─────────────────────────────────────────────────
# 마스킹 헬퍼 (Masking Helpers)
# ─────────────────────────────────────────────────

def _mask_payload(payload: Any) -> Any:
    """
    payload 재귀 순회 — 시크릿 키워드 감지 시 마스킹.

    defense-in-depth: 호출자가 실수로 시크릿 넣어도 출력에서 제거.
    """
    if isinstance(payload, dict):
        masked: dict[str, Any] = {}
        for k, v in payload.items():
            low = str(k).lower()
            if any(kw in low for kw in SECRET_KEYWORDS):
                masked[k] = MASKED_VALUE
            else:
                masked[k] = _mask_payload(v)
        return masked
    elif isinstance(payload, (list, tuple)):
        return [_mask_payload(item) for item in payload]
    else:
        return payload


def _json_default(o: Any) -> Any:
    """JSON encoder fallback — Decimal, datetime, Path 등."""
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, Path):
        return str(o)
    if isinstance(o, set):
        return list(o)
    raise TypeError(f"Type {type(o).__name__} not JSON serializable")


# ─────────────────────────────────────────────────
# 작성기 (Writer)
# ─────────────────────────────────────────────────

@dataclass
class AuditWriter:
    """
    통일 감사 로그 작성기.

    Args:
        audit_dir: 출력 디렉터리 (자동 생성)
        rotate_daily: 날짜별 파일 회전 (audit_YYYYMMDD.jsonl)
        max_payload_bytes: payload 크기 제한 (기본 64KB)
        fail_silently: 쓰기 실패 시 stderr만 — True (기본)
                       False면 raise

    파일명 (Filename):
        rotate_daily=True (기본):  audit_dir/audit_20260507.jsonl
        rotate_daily=False:        audit_dir/audit.jsonl
    """

    audit_dir: str
    rotate_daily: bool = True
    max_payload_bytes: int = 65536  # 64KB
    fail_silently: bool = True

    # 내부 상태
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _ensured_dirs: set[str] = field(default_factory=set, init=False, repr=False)

    def __post_init__(self):
        if not self.audit_dir:
            raise ValueError("audit_dir must be non-empty")

    # ─────────────────────────────────────────
    # 메인 API: write
    # ─────────────────────────────────────────

    def write(
        self,
        *,
        event_type: str,
        ctx: TraceContext,
        payload: Optional[dict[str, Any]] = None,
        severity: str = SEVERITY_INFO,
        timestamp_utc: Optional[datetime] = None,
    ) -> bool:
        """
        감사 이벤트 기록.

        Args:
            event_type: 이벤트 종류 (ALLOWED_EVENT_TYPES)
            ctx: TraceContext
            payload: 이벤트 데이터 (시크릿 자동 마스킹)
            severity: debug/info/warning/error/critical
            timestamp_utc: 명시 시각 (None이면 now)

        Returns:
            True: 성공, False: 실패 (fail_silently=True 시)
        """
        try:
            # ─── 검증 ──────────────────────────
            if event_type not in ALLOWED_EVENT_TYPES:
                # 표준 외 이벤트는 'other'로 폴백
                payload = {**(payload or {}), "_original_event_type": event_type}
                event_type = "other"
            if severity not in ALLOWED_SEVERITIES:
                severity = SEVERITY_INFO
            if not isinstance(ctx, TraceContext):
                raise TypeError(f"ctx must be TraceContext, got {type(ctx).__name__}")

            ts = timestamp_utc or datetime.now(timezone.utc)
            if ts.tzinfo is None:
                raise ValueError("timestamp_utc must be tz-aware")

            # ─── 레코드 조립 ───────────────────
            masked_payload = _mask_payload(payload or {})
            record = {
                "timestamp_utc": ts.isoformat(),
                "event_type": event_type,
                "severity": severity,
                "trace": ctx.to_audit_dict(),
                "payload": masked_payload,
            }

            # ─── 직렬화 + 크기 검증 ────────────
            line = json.dumps(record, ensure_ascii=False, default=_json_default)
            if len(line.encode("utf-8")) > self.max_payload_bytes:
                # 너무 크면 truncate + warning
                truncated_payload = {
                    "_truncated": True,
                    "_original_size_bytes": len(line.encode("utf-8")),
                    "_max_bytes": self.max_payload_bytes,
                    "_keys": list(masked_payload.keys()) if isinstance(masked_payload, dict) else None,
                }
                record["payload"] = truncated_payload
                record["severity"] = SEVERITY_WARNING
                line = json.dumps(record, ensure_ascii=False, default=_json_default)

            # ─── 파일 쓰기 ─────────────────────
            path = self._target_path(ts)
            self._ensure_dir(path.parent)

            with self._lock:
                with path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
                    f.flush()
            return True

        except Exception as e:  # noqa: BLE001
            err_msg = f"[AuditWriter] FAILED to write event_type={event_type}: {type(e).__name__}: {e}"
            if self.fail_silently:
                # stderr fallback (절대 silent 실패 안 함)
                print(err_msg, file=sys.stderr)
                return False
            raise

    # ─────────────────────────────────────────
    # 편의 메서드 (Convenience)
    # ─────────────────────────────────────────

    def write_signal(self, ctx: TraceContext, payload: dict, **kwargs) -> bool:
        return self.write(event_type="signal_generated", ctx=ctx, payload=payload, **kwargs)

    def write_risk(self, ctx: TraceContext, payload: dict, **kwargs) -> bool:
        return self.write(event_type="risk_evaluation", ctx=ctx, payload=payload, **kwargs)

    def write_order(self, ctx: TraceContext, payload: dict, *, submitted: bool = False, **kwargs) -> bool:
        et = "order_submitted" if submitted else "order_intent"
        return self.write(event_type=et, ctx=ctx, payload=payload, **kwargs)

    def write_fill(self, ctx: TraceContext, payload: dict, **kwargs) -> bool:
        return self.write(event_type="fill_received", ctx=ctx, payload=payload, **kwargs)

    def write_approval_request(self, ctx: TraceContext, payload: dict, **kwargs) -> bool:
        return self.write(event_type="approval_request", ctx=ctx, payload=payload, **kwargs)

    def write_approval_decision(self, ctx: TraceContext, payload: dict, **kwargs) -> bool:
        return self.write(event_type="approval_decision", ctx=ctx, payload=payload, **kwargs)

    def write_mcp_call(self, ctx: TraceContext, payload: dict, **kwargs) -> bool:
        return self.write(event_type="mcp_tool_call", ctx=ctx, payload=payload, **kwargs)

    def write_mcp_result(self, ctx: TraceContext, payload: dict, **kwargs) -> bool:
        return self.write(event_type="mcp_tool_result", ctx=ctx, payload=payload, **kwargs)

    def write_agent_decision(self, ctx: TraceContext, payload: dict, **kwargs) -> bool:
        return self.write(event_type="agent_decision", ctx=ctx, payload=payload, **kwargs)

    def write_exception(
        self,
        ctx: TraceContext,
        exc: Exception,
        *,
        additional: Optional[dict] = None,
        **kwargs,
    ) -> bool:
        payload = {
            "exception_type": type(exc).__name__,
            "message": str(exc),
            **(additional or {}),
        }
        return self.write(
            event_type="exception",
            ctx=ctx,
            payload=payload,
            severity=kwargs.pop("severity", SEVERITY_ERROR),
            **kwargs,
        )

    # ─────────────────────────────────────────
    # 내부 (Private)
    # ─────────────────────────────────────────

    def _target_path(self, ts: datetime) -> Path:
        """이벤트 시각 → 파일 경로 (날짜별 회전)."""
        if self.rotate_daily:
            date_str = ts.strftime("%Y%m%d")
            return Path(self.audit_dir) / f"audit_{date_str}.jsonl"
        return Path(self.audit_dir) / "audit.jsonl"

    def _ensure_dir(self, d: Path) -> None:
        """디렉터리 생성 (캐싱)."""
        key = str(d)
        if key in self._ensured_dirs:
            return
        d.mkdir(parents=True, exist_ok=True)
        self._ensured_dirs.add(key)


# ─────────────────────────────────────────────────
# 글로벌 기본 작성기 (Default Writer)
# ─────────────────────────────────────────────────

_DEFAULT_WRITER: Optional[AuditWriter] = None
_DEFAULT_LOCK = threading.Lock()


def configure_default_writer(audit_dir: str, **kwargs) -> AuditWriter:
    """기본 작성기 설정 — 앱 시작 시 1회 호출."""
    global _DEFAULT_WRITER
    with _DEFAULT_LOCK:
        _DEFAULT_WRITER = AuditWriter(audit_dir=audit_dir, **kwargs)
    return _DEFAULT_WRITER


def get_default_writer() -> Optional[AuditWriter]:
    """기본 작성기 반환 — 미설정 시 None."""
    return _DEFAULT_WRITER


def reset_default_writer() -> None:
    """기본 작성기 리셋 — 테스트용."""
    global _DEFAULT_WRITER
    with _DEFAULT_LOCK:
        _DEFAULT_WRITER = None
