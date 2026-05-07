"""
승인 저장소 (Approval Store)
==============================

JCPR Trading System - jcpr-ts-v01
Task 35 v0.1

SQLite 기반 승인 상태 저장소. 멀티 프로세스 안전, write-lock 사용.
(SQLite-backed approval store. Multi-process safe via write locks.)

설계 (Design):
    - 단일 파일 SQLite (로컬, 외부 노출 없음)
    - WAL 모드 (동시 read 성능)
    - 모든 상태 전이는 single transaction
    - 자동 만료 처리 (expired status)
    - approval_id 충돌 시 즉시 거부 (UUID4 기반)
    - self-approval 차단 (requested_by != decided_by)

상태 머신 (State Machine):
    pending ─[approve]→ approved ─[execute]→ executed
    pending ─[reject]→  rejected
    pending ─[cancel]→  cancelled  (요청자 본인이 취소)
    pending ─[expire]→  expired    (TTL 만료, 자동)
    approved ─[expire]→ expired    (승인 후 실행 TTL 만료)
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional


# ─────────────────────────────────────────────────
# 상태 (Status)
# ─────────────────────────────────────────────────

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_EXECUTED = "executed"
STATUS_EXPIRED = "expired"
STATUS_CANCELLED = "cancelled"

ALL_STATUSES = (
    STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED,
    STATUS_EXECUTED, STATUS_EXPIRED, STATUS_CANCELLED,
)

TERMINAL_STATUSES = (
    STATUS_REJECTED, STATUS_EXECUTED, STATUS_EXPIRED, STATUS_CANCELLED,
)


# ─────────────────────────────────────────────────
# Action 타입 (Action Types)
# ─────────────────────────────────────────────────

ACTION_SUBMIT_ORDER = "submit_order"
ACTION_CANCEL_ORDER = "cancel_order"
ACTION_SET_CAPACITY = "set_capacity"
ACTION_KILL_SWITCH = "kill_switch"

ALLOWED_ACTIONS = (
    ACTION_SUBMIT_ORDER, ACTION_CANCEL_ORDER,
    ACTION_SET_CAPACITY, ACTION_KILL_SWITCH,
)


# ─────────────────────────────────────────────────
# ID 생성 (ID Generators)
# ─────────────────────────────────────────────────

def generate_approval_id(*, now_utc: Optional[datetime] = None) -> str:
    """approval_id 생성: apv-YYYYMMDD-XXXXXXXX (hex)."""
    now = now_utc or datetime.now(timezone.utc)
    date_str = now.strftime("%Y%m%d")
    short = uuid.uuid4().hex[:8]
    return f"apv-{date_str}-{short}"


# ─────────────────────────────────────────────────
# 데이터 모델 (Data Model)
# ─────────────────────────────────────────────────

@dataclass(frozen=True)
class ApprovalRecord:
    """승인 레코드 (immutable snapshot)."""
    approval_id: str
    action_type: str
    requested_by: str
    requested_at_utc: datetime
    expires_at_utc: datetime
    payload: dict[str, Any]
    status: str
    decided_at_utc: Optional[datetime]
    decided_by: Optional[str]
    decision_reason: Optional[str]
    executed_at_utc: Optional[datetime]
    execution_result: Optional[dict[str, Any]]
    trace_id: str
    parent_trace_id: Optional[str]
    paper_mode: bool

    def to_dict(self) -> dict[str, Any]:
        def _iso(dt: Optional[datetime]) -> Optional[str]:
            return dt.isoformat() if dt else None
        return {
            "approval_id": self.approval_id,
            "action_type": self.action_type,
            "requested_by": self.requested_by,
            "requested_at_utc": _iso(self.requested_at_utc),
            "expires_at_utc": _iso(self.expires_at_utc),
            "payload": self.payload,
            "status": self.status,
            "decided_at_utc": _iso(self.decided_at_utc),
            "decided_by": self.decided_by,
            "decision_reason": self.decision_reason,
            "executed_at_utc": _iso(self.executed_at_utc),
            "execution_result": self.execution_result,
            "trace_id": self.trace_id,
            "parent_trace_id": self.parent_trace_id,
            "paper_mode": self.paper_mode,
        }


# ─────────────────────────────────────────────────
# 예외 (Exceptions)
# ─────────────────────────────────────────────────

class ApprovalStoreError(Exception):
    """승인 저장소 오류."""


class ApprovalNotFound(ApprovalStoreError):
    """approval_id 없음."""


class ApprovalStateError(ApprovalStoreError):
    """상태 전이 오류 (이미 결정됨, 만료 등)."""


class SelfApprovalError(ApprovalStoreError):
    """self-approval 차단."""


# ─────────────────────────────────────────────────
# DB 스키마 (Schema)
# ─────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    action_type TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    requested_at_utc TEXT NOT NULL,
    expires_at_utc TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    decided_at_utc TEXT,
    decided_by TEXT,
    decision_reason TEXT,
    executed_at_utc TEXT,
    execution_result_json TEXT,
    trace_id TEXT NOT NULL,
    parent_trace_id TEXT,
    paper_mode INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_status ON approvals(status);
CREATE INDEX IF NOT EXISTS idx_requested_at ON approvals(requested_at_utc);
CREATE INDEX IF NOT EXISTS idx_action_type ON approvals(action_type);
"""


# ─────────────────────────────────────────────────
# 저장소 (Store)
# ─────────────────────────────────────────────────

@dataclass
class ApprovalStore:
    """
    SQLite 승인 저장소.

    Args:
        db_path: SQLite 파일 경로 (자동 생성)
        default_ttl_seconds: 기본 TTL (요청 후 만료까지, 기본 300=5분)
        execute_ttl_seconds: 승인 후 실행 TTL (기본 60=1분)
        allow_self_approval: 같은 사용자 self-approval 허용 (기본 False — 보안)
    """

    db_path: str
    default_ttl_seconds: int = 300
    execute_ttl_seconds: int = 60
    allow_self_approval: bool = False

    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self):
        if self.default_ttl_seconds < 5 or self.default_ttl_seconds > 86400:
            raise ValueError(
                f"default_ttl_seconds must be 5..86400, got {self.default_ttl_seconds}"
            )
        if self.execute_ttl_seconds < 5 or self.execute_ttl_seconds > 3600:
            raise ValueError(
                f"execute_ttl_seconds must be 5..3600, got {self.execute_ttl_seconds}"
            )
        # 디렉터리 생성
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        # 스키마 초기화
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            # WAL 모드 (동시 read 안정)
            conn.execute("PRAGMA journal_mode=WAL")

    def _connect(self) -> sqlite3.Connection:
        """connection — 호출자가 close."""
        conn = sqlite3.connect(
            self.db_path,
            isolation_level=None,  # autocommit (manual BEGIN)
            timeout=10.0,
        )
        conn.row_factory = sqlite3.Row
        return conn

    # ─────────────────────────────────────────
    # 생성 (Create — Request)
    # ─────────────────────────────────────────

    def create_request(
        self,
        *,
        action_type: str,
        requested_by: str,
        payload: dict[str, Any],
        trace_id: str,
        parent_trace_id: Optional[str] = None,
        paper_mode: bool = True,
        custom_ttl_seconds: Optional[int] = None,
        now_utc: Optional[datetime] = None,
    ) -> ApprovalRecord:
        """승인 요청 생성 — pending 상태."""
        if action_type not in ALLOWED_ACTIONS:
            raise ApprovalStoreError(
                f"action_type '{action_type}' not allowed — {ALLOWED_ACTIONS}"
            )
        if not requested_by or not isinstance(requested_by, str):
            raise ApprovalStoreError("requested_by must be non-empty str")

        now = now_utc or datetime.now(timezone.utc)
        ttl = custom_ttl_seconds or self.default_ttl_seconds
        if ttl < 5 or ttl > 86400:
            raise ApprovalStoreError(f"ttl_seconds out of range: {ttl}")

        approval_id = generate_approval_id(now_utc=now)
        expires = now + timedelta(seconds=ttl)
        payload_json = json.dumps(payload, default=str, ensure_ascii=False)

        with self._lock:
            with self._connect() as conn:
                # PRIMARY KEY 충돌 시 IntegrityError
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("""
                    INSERT INTO approvals (
                        approval_id, action_type, requested_by,
                        requested_at_utc, expires_at_utc, payload_json,
                        status, trace_id, parent_trace_id, paper_mode
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    approval_id, action_type, requested_by,
                    now.isoformat(), expires.isoformat(), payload_json,
                    STATUS_PENDING, trace_id, parent_trace_id,
                    1 if paper_mode else 0,
                ))
                conn.execute("COMMIT")

        return self.get(approval_id)

    # ─────────────────────────────────────────
    # 결정 (Decide — Approve / Reject)
    # ─────────────────────────────────────────

    def approve(
        self,
        approval_id: str,
        *,
        decided_by: str,
        reason: str = "",
        now_utc: Optional[datetime] = None,
    ) -> ApprovalRecord:
        """승인 — pending → approved.

        Self-approval 차단 (allow_self_approval=False 시).
        """
        return self._decide(
            approval_id, STATUS_APPROVED,
            decided_by=decided_by, reason=reason, now_utc=now_utc,
        )

    def reject(
        self,
        approval_id: str,
        *,
        decided_by: str,
        reason: str = "",
        now_utc: Optional[datetime] = None,
    ) -> ApprovalRecord:
        """거부 — pending → rejected."""
        return self._decide(
            approval_id, STATUS_REJECTED,
            decided_by=decided_by, reason=reason, now_utc=now_utc,
        )

    def _decide(
        self,
        approval_id: str,
        new_status: str,
        *,
        decided_by: str,
        reason: str,
        now_utc: Optional[datetime],
    ) -> ApprovalRecord:
        if not decided_by or not isinstance(decided_by, str):
            raise ApprovalStoreError("decided_by must be non-empty str")

        now = now_utc or datetime.now(timezone.utc)

        with self._lock:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                # 1. 만료 처리 (모든 작업 전에 자동)
                self._auto_expire(conn, now)

                # 2. 현재 상태 확인
                row = conn.execute(
                    "SELECT * FROM approvals WHERE approval_id = ?",
                    (approval_id,),
                ).fetchone()
                if row is None:
                    conn.execute("ROLLBACK")
                    raise ApprovalNotFound(approval_id)
                if row["status"] != STATUS_PENDING:
                    conn.execute("ROLLBACK")
                    raise ApprovalStateError(
                        f"approval {approval_id} status={row['status']} — "
                        f"only pending can be decided"
                    )

                # 3. self-approval 검증
                if (not self.allow_self_approval
                        and row["requested_by"] == decided_by):
                    conn.execute("ROLLBACK")
                    raise SelfApprovalError(
                        f"requester '{decided_by}' cannot self-approve "
                        f"approval {approval_id}"
                    )

                # 4. 상태 업데이트
                conn.execute("""
                    UPDATE approvals SET
                        status = ?,
                        decided_at_utc = ?,
                        decided_by = ?,
                        decision_reason = ?
                    WHERE approval_id = ?
                """, (
                    new_status, now.isoformat(),
                    decided_by, reason, approval_id,
                ))
                conn.execute("COMMIT")

        return self.get(approval_id)

    # ─────────────────────────────────────────
    # 실행 (Execute)
    # ─────────────────────────────────────────

    def mark_executed(
        self,
        approval_id: str,
        *,
        execution_result: dict[str, Any],
        executed_by: str,
        now_utc: Optional[datetime] = None,
    ) -> ApprovalRecord:
        """approved → executed (idempotent — single use)."""
        if not executed_by:
            raise ApprovalStoreError("executed_by required")

        now = now_utc or datetime.now(timezone.utc)
        result_json = json.dumps(execution_result, default=str, ensure_ascii=False)

        with self._lock:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                # 자동 만료
                self._auto_expire(conn, now)

                row = conn.execute(
                    "SELECT * FROM approvals WHERE approval_id = ?",
                    (approval_id,),
                ).fetchone()
                if row is None:
                    conn.execute("ROLLBACK")
                    raise ApprovalNotFound(approval_id)
                if row["status"] != STATUS_APPROVED:
                    conn.execute("ROLLBACK")
                    raise ApprovalStateError(
                        f"approval {approval_id} status={row['status']} — "
                        f"only approved can be executed"
                    )
                # 승인 후 실행 TTL 검증
                decided_at = datetime.fromisoformat(row["decided_at_utc"])
                if (now - decided_at).total_seconds() > self.execute_ttl_seconds:
                    # 만료 처리
                    conn.execute("""
                        UPDATE approvals SET status = ?
                        WHERE approval_id = ?
                    """, (STATUS_EXPIRED, approval_id))
                    conn.execute("COMMIT")
                    raise ApprovalStateError(
                        f"approval {approval_id} expired — "
                        f"execute_ttl {self.execute_ttl_seconds}s exceeded"
                    )

                # 실행 마킹
                conn.execute("""
                    UPDATE approvals SET
                        status = ?,
                        executed_at_utc = ?,
                        execution_result_json = ?
                    WHERE approval_id = ?
                """, (
                    STATUS_EXECUTED, now.isoformat(),
                    result_json, approval_id,
                ))
                conn.execute("COMMIT")

        return self.get(approval_id)

    # ─────────────────────────────────────────
    # 취소 (Cancel — 요청자 본인)
    # ─────────────────────────────────────────

    def cancel(
        self,
        approval_id: str,
        *,
        cancelled_by: str,
        reason: str = "",
        now_utc: Optional[datetime] = None,
    ) -> ApprovalRecord:
        """요청자 본인이 미승인 요청 취소 — pending → cancelled."""
        now = now_utc or datetime.now(timezone.utc)
        with self._lock:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                self._auto_expire(conn, now)

                row = conn.execute(
                    "SELECT * FROM approvals WHERE approval_id = ?",
                    (approval_id,),
                ).fetchone()
                if row is None:
                    conn.execute("ROLLBACK")
                    raise ApprovalNotFound(approval_id)
                if row["status"] != STATUS_PENDING:
                    conn.execute("ROLLBACK")
                    raise ApprovalStateError(
                        f"approval {approval_id} status={row['status']} — "
                        f"only pending can be cancelled"
                    )
                if row["requested_by"] != cancelled_by:
                    conn.execute("ROLLBACK")
                    raise ApprovalStoreError(
                        f"only requester can cancel — "
                        f"requested_by={row['requested_by']}, "
                        f"cancelled_by={cancelled_by}"
                    )
                conn.execute("""
                    UPDATE approvals SET
                        status = ?,
                        decided_at_utc = ?,
                        decided_by = ?,
                        decision_reason = ?
                    WHERE approval_id = ?
                """, (
                    STATUS_CANCELLED, now.isoformat(),
                    cancelled_by, reason, approval_id,
                ))
                conn.execute("COMMIT")
        return self.get(approval_id)

    # ─────────────────────────────────────────
    # 조회 (Query)
    # ─────────────────────────────────────────

    def get(self, approval_id: str) -> ApprovalRecord:
        """단일 조회 — 만료 자동 반영."""
        with self._lock:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                self._auto_expire(conn, datetime.now(timezone.utc))
                row = conn.execute(
                    "SELECT * FROM approvals WHERE approval_id = ?",
                    (approval_id,),
                ).fetchone()
                conn.execute("COMMIT")
                if row is None:
                    raise ApprovalNotFound(approval_id)
                return _row_to_record(row)

    def get_optional(self, approval_id: str) -> Optional[ApprovalRecord]:
        """없으면 None."""
        try:
            return self.get(approval_id)
        except ApprovalNotFound:
            return None

    def list_pending(self, *, limit: int = 100) -> list[ApprovalRecord]:
        """대기 중 목록."""
        return self.list_by_status([STATUS_PENDING], limit=limit)

    def list_by_status(
        self,
        statuses: list[str],
        *,
        limit: int = 100,
    ) -> list[ApprovalRecord]:
        """상태별 조회."""
        if not statuses:
            return []
        if any(s not in ALL_STATUSES for s in statuses):
            raise ApprovalStoreError(f"invalid status in {statuses}")
        if limit < 1 or limit > 10000:
            raise ApprovalStoreError(f"limit out of range: {limit}")

        with self._lock:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                self._auto_expire(conn, datetime.now(timezone.utc))
                placeholders = ",".join("?" for _ in statuses)
                rows = conn.execute(f"""
                    SELECT * FROM approvals
                    WHERE status IN ({placeholders})
                    ORDER BY requested_at_utc DESC
                    LIMIT ?
                """, (*statuses, limit)).fetchall()
                conn.execute("COMMIT")
        return [_row_to_record(r) for r in rows]

    def list_by_requester(
        self,
        requested_by: str,
        *,
        limit: int = 100,
    ) -> list[ApprovalRecord]:
        """요청자별 조회."""
        with self._lock:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                self._auto_expire(conn, datetime.now(timezone.utc))
                rows = conn.execute("""
                    SELECT * FROM approvals
                    WHERE requested_by = ?
                    ORDER BY requested_at_utc DESC
                    LIMIT ?
                """, (requested_by, limit)).fetchall()
                conn.execute("COMMIT")
        return [_row_to_record(r) for r in rows]

    # ─────────────────────────────────────────
    # 만료 처리 (Auto-expire)
    # ─────────────────────────────────────────

    def _auto_expire(self, conn: sqlite3.Connection, now_utc: datetime) -> None:
        """pending 중 expires_at 지난 항목을 expired로 마킹.
        호출자가 BEGIN 안에서 호출.
        """
        conn.execute("""
            UPDATE approvals SET
                status = ?,
                decided_at_utc = ?,
                decision_reason = 'auto_expired'
            WHERE status = ?
              AND expires_at_utc < ?
        """, (
            STATUS_EXPIRED, now_utc.isoformat(),
            STATUS_PENDING, now_utc.isoformat(),
        ))


# ─────────────────────────────────────────────────
# 행 → record 변환
# ─────────────────────────────────────────────────

def _row_to_record(row: sqlite3.Row) -> ApprovalRecord:
    """sqlite3.Row → ApprovalRecord."""
    def _parse_dt(s: Optional[str]) -> Optional[datetime]:
        if not s:
            return None
        return datetime.fromisoformat(s)

    payload: dict = {}
    if row["payload_json"]:
        try:
            payload = json.loads(row["payload_json"])
        except (json.JSONDecodeError, TypeError):
            payload = {}

    exec_result: Optional[dict] = None
    if row["execution_result_json"]:
        try:
            exec_result = json.loads(row["execution_result_json"])
        except (json.JSONDecodeError, TypeError):
            exec_result = None

    return ApprovalRecord(
        approval_id=row["approval_id"],
        action_type=row["action_type"],
        requested_by=row["requested_by"],
        requested_at_utc=_parse_dt(row["requested_at_utc"]),
        expires_at_utc=_parse_dt(row["expires_at_utc"]),
        payload=payload,
        status=row["status"],
        decided_at_utc=_parse_dt(row["decided_at_utc"]),
        decided_by=row["decided_by"],
        decision_reason=row["decision_reason"],
        executed_at_utc=_parse_dt(row["executed_at_utc"]),
        execution_result=exec_result,
        trace_id=row["trace_id"],
        parent_trace_id=row["parent_trace_id"],
        paper_mode=bool(row["paper_mode"]),
    )
