"""
실행 기록 (Execution Record)
=============================

JCPR Trading System - jcpr-ts-v01
Task 21 v0.1

ExecutionGateway의 모든 실행을 audit log + 멱등성 캐시로 기록.
(Records all executions for audit + idempotency cache.)

원칙 (Principles):
- UTC tz-aware datetime
- 비밀/계좌번호/토큰 등 절대 포함 안 함
- 24시간 쿨다운 — 동일 signal_id 재실행 차단
- 로컬 JSONL (Zone D)
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)

# 멱등성 쿨다운 (Idempotency cooldown)
DEFAULT_IDEMPOTENCY_WINDOW = timedelta(hours=24)


# ─────────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────────

class ExecutionStage(str, Enum):
    """실행 단계 — 어디서 종료되었는지 추적."""
    INIT = "init"
    STOP_CHECK = "stop_check"
    SIGNAL_VALIDATION = "signal_validation"
    IDEMPOTENCY_CHECK = "idempotency_check"
    ACCOUNT_SNAPSHOT = "account_snapshot"
    SIZING = "sizing"
    RISK_GATE = "risk_gate"
    APPROVAL = "approval"
    SUBMISSION = "submission"
    DONE = "done"


class ExecutionOutcome(str, Enum):
    """최종 결과."""
    SUBMITTED = "submitted"   # 주문 송신 성공 (dry-run 또는 live)
    REJECTED = "rejected"     # 어떤 단계에서 거부
    SKIPPED = "skipped"       # FLAT 시그널, 중복 등 행동 불필요
    ERROR = "error"           # 예상 못한 예외


# ─────────────────────────────────────────────────
# Signal ID 생성 — 멱등 키
# ─────────────────────────────────────────────────

def compute_signal_id(
    symbol: str,
    strategy_id: str,
    timestamp_utc: datetime,
    side: str,
) -> str:
    """
    시그널 멱등 키 생성.
    같은 (symbol, strategy, timestamp, side) → 같은 ID.
    """
    if timestamp_utc.tzinfo is None:
        raise ValueError("timestamp_utc tz-aware 필수")
    raw = f"{symbol}|{strategy_id}|{timestamp_utc.astimezone(timezone.utc).isoformat()}|{side}"
    return f"sig-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"


# ─────────────────────────────────────────────────
# ExecutionResult / Record
# ─────────────────────────────────────────────────

@dataclass(frozen=True)
class ExecutionResult:
    """
    호출자에게 반환되는 실행 결과.
    (Result returned to caller.)
    """
    execution_id: str
    signal_id: str
    outcome: ExecutionOutcome
    final_stage: ExecutionStage
    reject_reason: Optional[str] = None
    is_dry_run: Optional[bool] = None
    broker_order_no: Optional[str] = None
    quantity: Optional[int] = None
    aligned_price: Optional[Decimal] = None
    estimated_cost_krw: Optional[Decimal] = None
    started_at_utc: Optional[datetime] = None
    completed_at_utc: Optional[datetime] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def succeeded(self) -> bool:
        return self.outcome == ExecutionOutcome.SUBMITTED


def _json_default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, Enum):
        return o.value
    raise TypeError(f"JSON 직렬화 불가: {type(o)}")


# ─────────────────────────────────────────────────
# JSONL Audit Logger + Idempotency Cache
# ─────────────────────────────────────────────────

class ExecutionAuditLog:
    """
    JSONL 기반 실행 audit log + 멱등성 캐시.
    (JSONL-based execution audit log + idempotency cache.)

    캐시는 메모리 dict + 디스크 JSONL 모두 활용:
    - 시작 시 최근 N시간 JSONL 읽어 메모리에 로드
    - 새 실행은 메모리 즉시 갱신 + 디스크 append
    - signal_id 중복은 메모리에서 빠르게 검사
    """

    def __init__(
        self,
        path: str | Path,
        idempotency_window: timedelta = DEFAULT_IDEMPOTENCY_WINDOW,
    ):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._window = idempotency_window
        self._lock = threading.Lock()
        # signal_id → (executed_at_utc, outcome)
        self._cache: dict[str, tuple[datetime, ExecutionOutcome]] = {}
        self._load_recent()

    @property
    def path(self) -> Path:
        return self._path

    def _load_recent(self) -> None:
        """기존 JSONL에서 최근 window 이내 기록을 메모리 캐시로 로드."""
        if not self._path.exists():
            return
        cutoff = datetime.now(timezone.utc) - self._window
        try:
            with self._path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        sid = rec.get("signal_id")
                        ts_str = rec.get("completed_at_utc") or rec.get("started_at_utc")
                        outcome_str = rec.get("outcome")
                        if not sid or not ts_str or not outcome_str:
                            continue
                        ts = datetime.fromisoformat(ts_str)
                        if ts >= cutoff:
                            try:
                                outcome = ExecutionOutcome(outcome_str)
                            except ValueError:
                                continue
                            # 가장 최근 기록만 유지
                            existing = self._cache.get(sid)
                            if existing is None or existing[0] < ts:
                                self._cache[sid] = (ts, outcome)
                    except (json.JSONDecodeError, ValueError, KeyError):
                        continue
        except OSError as e:
            logger.warning("Audit log 로드 실패: %s", e)

    # ---------- Idempotency Check ----------

    def is_duplicate(self, signal_id: str, *, now_utc: Optional[datetime] = None) -> bool:
        """
        signal_id가 idempotency window 내 이미 실행되었는지.
        (Has signal_id been processed within idempotency window?)

        SKIPPED는 중복으로 간주하지 않음 (재시도 가능).
        """
        if now_utc is None:
            now_utc = datetime.now(timezone.utc)
        with self._lock:
            entry = self._cache.get(signal_id)
            if entry is None:
                return False
            executed_at, outcome = entry
            # SKIPPED는 중복 카운트 안 함 (재시도 허용)
            if outcome == ExecutionOutcome.SKIPPED:
                return False
            # 윈도우 만료
            if now_utc - executed_at > self._window:
                return False
            return True

    # ---------- Append ----------

    def write(self, record: dict[str, Any]) -> None:
        """실행 기록 1건 추가 (캐시 갱신 + 디스크 append)."""
        signal_id = record.get("signal_id")
        outcome_str = record.get("outcome")
        ts_str = record.get("completed_at_utc") or record.get("started_at_utc")

        try:
            line = json.dumps(record, default=_json_default, ensure_ascii=False) + "\n"
            with self._path.open("a", encoding="utf-8") as f:
                f.write(line)
        except OSError as e:
            logger.error("Audit log 기록 실패: %s", e)
            return

        # 캐시 갱신
        if signal_id and outcome_str and ts_str:
            try:
                ts = datetime.fromisoformat(ts_str) if isinstance(ts_str, str) else ts_str
                outcome = ExecutionOutcome(outcome_str) if isinstance(outcome_str, str) else outcome_str
                with self._lock:
                    self._cache[signal_id] = (ts, outcome)
            except (ValueError, TypeError):
                pass

    def cache_size(self) -> int:
        """현재 캐시 크기 (디버그용)."""
        with self._lock:
            return len(self._cache)


def new_execution_id() -> str:
    return f"exec-{uuid4().hex[:16]}"


def build_record(
    *,
    execution_id: str,
    signal_id: str,
    symbol: str,
    strategy_id: str,
    side: str,
    outcome: ExecutionOutcome,
    final_stage: ExecutionStage,
    started_at_utc: datetime,
    completed_at_utc: datetime,
    reject_reason: Optional[str] = None,
    is_dry_run: Optional[bool] = None,
    broker_order_no: Optional[str] = None,
    quantity: Optional[int] = None,
    aligned_price: Optional[Decimal] = None,
    estimated_cost_krw: Optional[Decimal] = None,
    stage_results: Optional[dict[str, Any]] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """
    audit log용 dict 빌드.
    (Build dict for audit log.)

    비밀 데이터 절대 포함 안 함.
    """
    return {
        "execution_id": execution_id,
        "signal_id": signal_id,
        "symbol": symbol,
        "strategy_id": strategy_id,
        "side": side,
        "outcome": outcome.value,
        "final_stage": final_stage.value,
        "reject_reason": reject_reason,
        "is_dry_run": is_dry_run,
        "broker_order_no": broker_order_no,
        "quantity": quantity,
        "aligned_price": str(aligned_price) if aligned_price is not None else None,
        "estimated_cost_krw": str(estimated_cost_krw) if estimated_cost_krw is not None else None,
        "started_at_utc": started_at_utc.astimezone(timezone.utc).isoformat(),
        "completed_at_utc": completed_at_utc.astimezone(timezone.utc).isoformat(),
        "stage_results": stage_results or {},
        "metadata": metadata or {},
    }
