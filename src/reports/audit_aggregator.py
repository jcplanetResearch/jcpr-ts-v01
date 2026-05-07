"""
감사 로그 집계 (Audit Log Aggregator)
======================================

JCPR Trading System - jcpr-ts-v01
Task 49 v0.2

JSONL 형식 audit log를 집계하여 요약 통계 생성:
    - Task 19 risk_decisions.jsonl  → RiskAuditStats
    - Task 21 executions.jsonl       → ExecutionAuditStats
    - Task 40 approvals.jsonl        → ApprovalAuditStats

설계 원칙 (Design Principles):
    - 빈 파일/없는 파일에 대해 graceful 처리 (no exception)
    - 빈 줄·잘못된 JSON 자동 skip
    - 시간대 필터 지원 (since_utc / until_utc)
    - 모든 datetime UTC tz-aware
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


# ─────────────────────────────────────────────────
# 데이터 모델 (Data Models)
# ─────────────────────────────────────────────────

@dataclass(frozen=True)
class RiskAuditStats:
    """리스크 게이트 감사 통계 (Task 19)."""
    total_evaluations: int = 0
    approved: int = 0
    rejected: int = 0
    rejection_rate: float = 0.0
    by_gate: dict[str, int] = field(default_factory=dict)
    by_reason: dict[str, int] = field(default_factory=dict)
    by_symbol_rejected: dict[str, int] = field(default_factory=dict)
    by_strategy_rejected: dict[str, int] = field(default_factory=dict)
    sample_rejections: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class ExecutionAuditStats:
    """실행 게이트웨이 감사 통계 (Task 21)."""
    total_executions: int = 0
    success: int = 0
    error: int = 0
    cancelled: int = 0
    error_rate: float = 0.0
    by_stage: dict[str, int] = field(default_factory=dict)
    error_messages: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class ApprovalAuditStats:
    """승인 워크플로우 감사 통계 (Task 40)."""
    total_requests: int = 0
    approved: int = 0
    declined: int = 0
    auto_approved: int = 0
    timeout: int = 0
    approval_rate: float = 0.0


# ─────────────────────────────────────────────────
# JSONL 헬퍼 (JSONL Helper)
# ─────────────────────────────────────────────────

def _iter_jsonl(
    path: Path,
    *,
    since_utc: Optional[datetime] = None,
    until_utc: Optional[datetime] = None,
    time_field: str = "timestamp_utc",
    max_lines: int = 100_000,
):
    """JSONL 파일 line-by-line iterator — 빈 줄·파싱 실패 skip."""
    if not path.exists():
        return
    try:
        with path.open("r", encoding="utf-8") as f:
            for i, raw in enumerate(f):
                if i >= max_lines:
                    break
                line = raw.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                # 시간 필터
                if (since_utc or until_utc) and time_field in rec:
                    try:
                        ts = datetime.fromisoformat(
                            str(rec[time_field]).replace("Z", "+00:00")
                        )
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                        if since_utc and ts < since_utc:
                            continue
                        if until_utc and ts > until_utc:
                            continue
                    except (ValueError, AttributeError):
                        pass
                yield rec
    except OSError:
        return


def _bump(d: dict[str, int], key: Optional[str]) -> None:
    """카운터 증가 — None/빈 키는 'unknown'."""
    k = str(key) if key not in (None, "") else "unknown"
    d[k] = d.get(k, 0) + 1


# ─────────────────────────────────────────────────
# 리스크 감사 집계 (Risk Audit)
# ─────────────────────────────────────────────────

def aggregate_risk_audit(
    path: Optional[str | Path],
    *,
    session_start_utc: Optional[datetime] = None,
    session_end_utc: Optional[datetime] = None,
    sample_size: int = 10,
) -> RiskAuditStats:
    """Task 19 risk_decisions.jsonl 집계."""
    if not path:
        return RiskAuditStats()
    p = Path(path)

    total = 0
    approved = 0
    rejected = 0
    by_gate: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    by_symbol: dict[str, int] = {}
    by_strategy: dict[str, int] = {}
    samples: list[dict[str, Any]] = []

    for rec in _iter_jsonl(
        p,
        since_utc=session_start_utc,
        until_utc=session_end_utc,
        time_field="evaluated_at_utc",
    ):
        total += 1
        decision = rec.get("decision", "")
        if decision == "approve":
            approved += 1
        elif decision == "reject":
            rejected += 1
            _bump(by_gate, rec.get("rejected_gate"))
            _bump(by_reason, rec.get("rejection_reason"))
            _bump(by_symbol, rec.get("symbol"))
            _bump(by_strategy, rec.get("strategy_id") or rec.get("strategy"))
            if len(samples) < sample_size:
                # 시크릿성 키 자동 제거
                safe = {
                    k: v for k, v in rec.items()
                    if not any(s in str(k).lower() for s in ("secret", "token", "key", "password", "auth"))
                }
                samples.append(safe)

    rate = (rejected / total) if total > 0 else 0.0
    return RiskAuditStats(
        total_evaluations=total,
        approved=approved,
        rejected=rejected,
        rejection_rate=rate,
        by_gate=by_gate,
        by_reason=by_reason,
        by_symbol_rejected=by_symbol,
        by_strategy_rejected=by_strategy,
        sample_rejections=samples,
    )


# ─────────────────────────────────────────────────
# 실행 감사 집계 (Execution Audit)
# ─────────────────────────────────────────────────

def aggregate_execution_audit(
    path: Optional[str | Path],
    *,
    session_start_utc: Optional[datetime] = None,
    session_end_utc: Optional[datetime] = None,
    error_sample_size: int = 20,
) -> ExecutionAuditStats:
    """Task 21 executions.jsonl 집계."""
    if not path:
        return ExecutionAuditStats()
    p = Path(path)

    total = 0
    success = 0
    error = 0
    cancelled = 0
    by_stage: dict[str, int] = {}
    errors: list[dict[str, Any]] = []

    for rec in _iter_jsonl(
        p,
        since_utc=session_start_utc,
        until_utc=session_end_utc,
        time_field="started_at_utc",
    ):
        total += 1
        outcome = rec.get("outcome", "")
        if outcome == "success" or outcome == "filled":
            success += 1
        elif outcome == "error":
            error += 1
            stage = rec.get("stage") or rec.get("error_stage")
            _bump(by_stage, stage)
            if len(errors) < error_sample_size:
                errors.append({
                    "execution_id": rec.get("execution_id"),
                    "symbol": rec.get("symbol"),
                    "stage": stage,
                    "message": rec.get("error") or rec.get("error_message", ""),
                    "started_at_utc": rec.get("started_at_utc"),
                })
        elif outcome == "cancelled":
            cancelled += 1

    rate = (error / total) if total > 0 else 0.0
    return ExecutionAuditStats(
        total_executions=total,
        success=success,
        error=error,
        cancelled=cancelled,
        error_rate=rate,
        by_stage=by_stage,
        error_messages=errors,
    )


# ─────────────────────────────────────────────────
# 승인 감사 집계 (Approval Audit)
# ─────────────────────────────────────────────────

def aggregate_approval_audit(
    path: Optional[str | Path],
    *,
    session_start_utc: Optional[datetime] = None,
    session_end_utc: Optional[datetime] = None,
) -> ApprovalAuditStats:
    """Task 40 approvals.jsonl 집계."""
    if not path:
        return ApprovalAuditStats()
    p = Path(path)

    total = 0
    approved = 0
    declined = 0
    auto = 0
    timeout = 0

    for rec in _iter_jsonl(
        p,
        since_utc=session_start_utc,
        until_utc=session_end_utc,
        time_field="requested_at_utc",
    ):
        total += 1
        outcome = rec.get("outcome", "")
        if outcome == "approved":
            approved += 1
            if rec.get("auto_approved", False):
                auto += 1
        elif outcome == "declined":
            declined += 1
        elif outcome == "timeout":
            timeout += 1

    rate = (approved / total) if total > 0 else 0.0
    return ApprovalAuditStats(
        total_requests=total,
        approved=approved,
        declined=declined,
        auto_approved=auto,
        timeout=timeout,
        approval_rate=rate,
    )
