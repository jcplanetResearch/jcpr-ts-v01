"""
Audit Log 집계기 (Audit Log Aggregator)
========================================

JCPR Trading System - jcpr-ts-v01
Task 49 v0.1

Task 19 (risk), Task 21 (execution), Task 40 (approval) JSONL audit log을
일별 통계로 집계.

원칙:
- Read-only — JSONL 변경 없음
- 비밀 누출 금지 — secret/token/account_no 키워드 자동 무시
- 시간 필터 (since_utc / until_utc)
- 깨진 JSONL 라인은 skip + warning
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 비밀 누출 방지 키워드 — 키 이름에 포함 시 값 마스킹
_SECRET_KEYWORDS = ("secret", "token", "password", "app_key", "account_no")


def _is_secret_key(key: str) -> bool:
    return any(kw in key.lower() for kw in _SECRET_KEYWORDS)


def _safe_dict(d: dict[str, Any]) -> dict[str, Any]:
    """비밀 키 마스킹된 dict 반환 (얕은 복사)."""
    return {k: ("[REDACTED]" if _is_secret_key(k) else v) for k, v in d.items()}


def _parse_iso(s: Any) -> Optional[datetime]:
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _within_window(
    dt: Optional[datetime],
    since_utc: Optional[datetime],
    until_utc: Optional[datetime],
) -> bool:
    if dt is None:
        # 시각 정보 없으면 포함 (보수적)
        return True
    if since_utc is not None and dt < since_utc:
        return False
    if until_utc is not None and dt > until_utc:
        return False
    return True


def _read_jsonl(
    path: Path,
    *,
    since_utc: Optional[datetime] = None,
    until_utc: Optional[datetime] = None,
    time_field: str = "decided_at_utc",
) -> list[dict[str, Any]]:
    """JSONL 파일을 읽어 dict 리스트 반환. 시간 필터 적용."""
    if not path.exists():
        logger.info("audit log 없음 (없는 것 정상): %s", path)
        return []

    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                logger.warning("audit log 파싱 실패 %s 라인 %d: %s", path.name, line_no, e)
                continue
            if not isinstance(rec, dict):
                continue

            ts = _parse_iso(rec.get(time_field))
            if not _within_window(ts, since_utc, until_utc):
                continue
            rows.append(rec)
    return rows


# ─────────────────────────────────────────────────
# Risk Audit (Task 19)
# ─────────────────────────────────────────────────

@dataclass(frozen=True)
class RiskAuditStats:
    """Task 19 risk_decisions.jsonl 집계."""
    total: int = 0
    pass_count: int = 0
    reject_count: int = 0
    by_gate_reject: dict[str, int] = field(default_factory=dict)
    by_symbol_reject: dict[str, int] = field(default_factory=dict)
    by_strategy_reject: dict[str, int] = field(default_factory=dict)
    rejection_rate: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "pass_count": self.pass_count,
            "reject_count": self.reject_count,
            "rejection_rate": round(self.rejection_rate, 4),
            "by_gate_reject": dict(self.by_gate_reject),
            "by_symbol_reject": dict(self.by_symbol_reject),
            "by_strategy_reject": dict(self.by_strategy_reject),
        }


def aggregate_risk_audit(
    path: str | Path,
    *,
    since_utc: Optional[datetime] = None,
    until_utc: Optional[datetime] = None,
) -> RiskAuditStats:
    """Task 19 risk audit log 집계."""
    rows = _read_jsonl(
        Path(path), since_utc=since_utc, until_utc=until_utc,
        time_field="decided_at_utc",
    )
    if not rows:
        return RiskAuditStats()

    pass_count = 0
    reject_count = 0
    gate_counter: Counter[str] = Counter()
    symbol_counter: Counter[str] = Counter()
    strategy_counter: Counter[str] = Counter()

    for rec in rows:
        decision = rec.get("decision") or rec.get("outcome")
        if decision == "pass":
            pass_count += 1
        elif decision == "reject":
            reject_count += 1
            # 어느 게이트에서 거부됐는지
            rejected_gate = (
                rec.get("rejected_by_gate")
                or rec.get("first_reject_gate")
                or rec.get("gate_name")
                or "unknown"
            )
            gate_counter[str(rejected_gate)] += 1

            # 종목/전략 분포
            sym = rec.get("symbol")
            if sym:
                symbol_counter[str(sym)] += 1
            strat = rec.get("strategy_id") or rec.get("strategy")
            if strat:
                strategy_counter[str(strat)] += 1

    total = pass_count + reject_count
    rate = (reject_count / total) if total > 0 else 0.0

    return RiskAuditStats(
        total=total,
        pass_count=pass_count,
        reject_count=reject_count,
        by_gate_reject=dict(gate_counter),
        by_symbol_reject=dict(symbol_counter),
        by_strategy_reject=dict(strategy_counter),
        rejection_rate=rate,
    )


# ─────────────────────────────────────────────────
# Execution Audit (Task 21)
# ─────────────────────────────────────────────────

@dataclass(frozen=True)
class ExecutionAuditStats:
    """Task 21 execution audit 집계."""
    total: int = 0
    submitted_count: int = 0
    rejected_count: int = 0
    error_count: int = 0
    dry_run_count: int = 0
    by_symbol: dict[str, int] = field(default_factory=dict)
    error_records: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "submitted_count": self.submitted_count,
            "rejected_count": self.rejected_count,
            "error_count": self.error_count,
            "dry_run_count": self.dry_run_count,
            "by_symbol": dict(self.by_symbol),
            "error_records": list(self.error_records),
        }


def aggregate_execution_audit(
    path: str | Path,
    *,
    since_utc: Optional[datetime] = None,
    until_utc: Optional[datetime] = None,
) -> ExecutionAuditStats:
    """Task 21 execution audit 집계."""
    rows = _read_jsonl(
        Path(path), since_utc=since_utc, until_utc=until_utc,
        time_field="started_at_utc",
    )
    if not rows:
        return ExecutionAuditStats()

    submitted = 0
    rejected = 0
    error = 0
    dry_run = 0
    by_symbol: Counter[str] = Counter()
    errors: list[dict[str, Any]] = []

    for rec in rows:
        outcome = rec.get("outcome")
        if outcome == "submitted":
            submitted += 1
        elif outcome == "rejected":
            rejected += 1
        elif outcome == "error":
            error += 1
            # 에러 정보 저장 (비밀 마스킹)
            errors.append({
                "execution_id": rec.get("execution_id"),
                "started_at_utc": rec.get("started_at_utc"),
                "symbol": rec.get("symbol"),
                "error": rec.get("error") or rec.get("error_message"),
                "stage": rec.get("stage"),
            })

        if rec.get("is_dry_run"):
            dry_run += 1

        sym = rec.get("symbol")
        if sym:
            by_symbol[str(sym)] += 1

    return ExecutionAuditStats(
        total=len(rows),
        submitted_count=submitted,
        rejected_count=rejected,
        error_count=error,
        dry_run_count=dry_run,
        by_symbol=dict(by_symbol),
        error_records=errors,
    )


# ─────────────────────────────────────────────────
# Approval Audit (Task 40)
# ─────────────────────────────────────────────────

@dataclass(frozen=True)
class ApprovalAuditStats:
    """Task 40 approval audit 집계."""
    total: int = 0
    approved_count: int = 0
    rejected_count: int = 0
    by_approver: dict[str, int] = field(default_factory=dict)
    avg_response_time_sec: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "approved_count": self.approved_count,
            "rejected_count": self.rejected_count,
            "by_approver": dict(self.by_approver),
            "avg_response_time_sec": (
                round(self.avg_response_time_sec, 2)
                if self.avg_response_time_sec is not None else None
            ),
        }


def aggregate_approval_audit(
    path: str | Path,
    *,
    since_utc: Optional[datetime] = None,
    until_utc: Optional[datetime] = None,
) -> ApprovalAuditStats:
    """Task 40 approval audit 집계."""
    rows = _read_jsonl(
        Path(path), since_utc=since_utc, until_utc=until_utc,
        time_field="decided_at_utc",
    )
    if not rows:
        return ApprovalAuditStats()

    approved = 0
    rejected = 0
    by_approver: Counter[str] = Counter()
    response_times: list[float] = []

    for rec in rows:
        if rec.get("approved"):
            approved += 1
        else:
            rejected += 1
        approver = rec.get("approver", "unknown")
        by_approver[str(approver)] += 1
        rt = rec.get("response_time_sec")
        if isinstance(rt, (int, float)) and rt >= 0:
            response_times.append(float(rt))

    avg_rt = sum(response_times) / len(response_times) if response_times else None

    return ApprovalAuditStats(
        total=len(rows),
        approved_count=approved,
        rejected_count=rejected,
        by_approver=dict(by_approver),
        avg_response_time_sec=avg_rt,
    )
