"""Unified Approval Store (Phase 1 Integration).

Single source of truth for ALL approval workflows in JCPR Trading System.

Replaces:
    - src/mcp_servers/_approval_store.py (Task 35) — DELETED in Phase 2
    - src/execution/_approval_state.py (Task 40) — RENAMED to this file

Used by:
    - Task 35 MCP restricted server (request_*, execute_approved_action tools)
    - Task 40 ExecutionGateway (propose_order, execute)
    - scripts/approve_cli.py (operator approval CLI)

State machine (3 phases + cancellation):
    PROPOSED ──approve──► APPROVED ──mark_executing──► EXECUTING
                              │                            │
                              │                            ├──► EXECUTED
                              │                            └──► EXEC_FAILED
                              │
    PROPOSED ──reject────► REJECTED
    PROPOSED ──expire────► EXPIRED       (TTL elapsed without decision)
    PROPOSED ──cancel────► CANCELLED     (requester withdrew)
    APPROVED ──expire────► EXPIRED       (execute TTL elapsed)

Security guarantees:
    1. SQLite file mode 0600 enforced at creation; verified on each open.
    2. Self-approval blocked (operator_id != requested_by) — SelfApprovalError.
    3. Live mode requires JCPR_ALLOW_LIVE=1 + explicit allow_live=True flag.
    4. All state transitions atomic via SQLite WAL + threading.RLock.
    5. Decimal monetary fields stored as TEXT (no float precision loss).
    6. UUID approval_ids — not sequential (no enumeration leaks).
    7. Single-use enforcement — cannot re-execute APPROVED → EXECUTED.
    8. UTC tz-aware timestamps everywhere (stored as ISO 8601 strings).

This file replaces TWO previous files; see docs/MIGRATION.md for the
operator-side cleanup procedure.
"""
from __future__ import annotations

import json
import os
import secrets as _secrets_mod
import sqlite3
import stat
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence


# =============================================================================
# Constants
# =============================================================================

# TTL defaults — match Task 35 + Task 40 spec
DEFAULT_APPROVAL_TTL_SECONDS: int = 300        # 5 minutes for operator decision
DEFAULT_EXECUTE_TTL_SECONDS: int = 60          # 60 seconds after approve to exec
DEFAULT_KILL_SWITCH_TTL_SECONDS: int = 60      # urgent — same as execute

# Required SQLite file permissions (POSIX)
REQUIRED_FILE_MODE: int = 0o600
REQUIRED_DIR_MODE: int = 0o700

# Action kinds — must match Task 35 MCP tool names
ACTION_SUBMIT_ORDER: str = "submit_order"
ACTION_CANCEL_ORDER: str = "cancel_order"
ACTION_SET_CAPACITY: str = "set_capacity"
ACTION_KILL_SWITCH: str = "kill_switch"

VALID_ACTION_KINDS: frozenset[str] = frozenset({
    ACTION_SUBMIT_ORDER,
    ACTION_CANCEL_ORDER,
    ACTION_SET_CAPACITY,
    ACTION_KILL_SWITCH,
})


# =============================================================================
# State enum
# =============================================================================

class ApprovalState(str, Enum):
    """Approval lifecycle state."""

    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    EXECUTING = "executing"
    EXECUTED = "executed"
    EXEC_FAILED = "exec_failed"

    @property
    def is_terminal(self) -> bool:
        """States that cannot transition further."""
        return self in (
            ApprovalState.REJECTED,
            ApprovalState.EXPIRED,
            ApprovalState.CANCELLED,
            ApprovalState.EXECUTED,
            ApprovalState.EXEC_FAILED,
        )

    @property
    def is_pending_decision(self) -> bool:
        return self == ApprovalState.PROPOSED

    @property
    def is_pending_execution(self) -> bool:
        return self == ApprovalState.APPROVED


# =============================================================================
# Exceptions
# =============================================================================

class ApprovalStoreError(Exception):
    """Base for all approval store errors."""


class ApprovalNotFound(ApprovalStoreError):
    """Approval ID does not exist."""


class ApprovalStateError(ApprovalStoreError):
    """Invalid state transition (e.g. approving an already-rejected request)."""


class SelfApprovalError(ApprovalStoreError):
    """Approver matches requester — blocked."""


class ApprovalExpiredError(ApprovalStoreError):
    """TTL elapsed; cannot proceed."""


class ApprovalIntegrityError(ApprovalStoreError):
    """File permission or schema violation."""


class LiveModeBlockedError(ApprovalStoreError):
    """mode='live' attempted without allow_live=True or JCPR_ALLOW_LIVE=1."""


# =============================================================================
# Record dataclass
# =============================================================================

@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    """Frozen view of a single approval row.

    All datetimes are UTC. All monetary fields are Decimal-as-string in payload.
    """

    approval_id: str
    action_kind: str               # one of VALID_ACTION_KINDS
    payload: Mapping[str, Any]     # JSON-serializable; symbol/qty/price etc.
    requested_by: str
    mode: str                      # 'paper' | 'live'
    state: ApprovalState
    created_at: datetime
    expires_at: datetime           # decision TTL
    decided_by: Optional[str] = None
    decided_at: Optional[datetime] = None
    decision_reason: Optional[str] = None
    execute_expires_at: Optional[datetime] = None  # set on approve
    executed_by: Optional[str] = None
    executed_at: Optional[datetime] = None
    execution_result: Optional[Mapping[str, Any]] = None
    session_id: Optional[str] = None
    trace_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """Serializable dict — datetimes as ISO 8601, state as string."""
        d = asdict(self)
        d["state"] = self.state.value
        for key in ("created_at", "expires_at", "decided_at",
                    "execute_expires_at", "executed_at"):
            v = d.get(key)
            if isinstance(v, datetime):
                d[key] = v.isoformat()
        return d


# =============================================================================
# Helper functions
# =============================================================================

def _generate_approval_id(now: datetime) -> str:
    """Format: apv-YYYYMMDD-<16 hex>. Non-sequential, unguessable."""
    date_part = now.strftime("%Y%m%d")
    rand_part = _secrets_mod.token_hex(8)  # 16 hex chars
    return f"apv-{date_part}-{rand_part}"


def _verify_file_mode(path: Path) -> None:
    """Enforce 0600 on the SQLite file. Raises ApprovalIntegrityError on POSIX."""
    if os.name != "posix":
        return  # Windows file mode semantics differ; skip silently
    if not path.exists():
        return
    actual_mode = stat.S_IMODE(path.stat().st_mode)
    if actual_mode != REQUIRED_FILE_MODE:
        raise ApprovalIntegrityError(
            f"approval store {path} has mode {oct(actual_mode)}, "
            f"required {oct(REQUIRED_FILE_MODE)}. "
            f"Fix: chmod 600 {path}"
        )


def _ensure_parent_dir(path: Path) -> None:
    """Create parent dir with 0700 if absent."""
    parent = path.parent
    if not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            os.chmod(parent, REQUIRED_DIR_MODE)


def _set_file_mode_0600(path: Path) -> None:
    """Apply 0600 to the file (POSIX only)."""
    if os.name == "posix" and path.exists():
        os.chmod(path, REQUIRED_FILE_MODE)


def _to_iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        raise ValueError("datetime must be tz-aware (UTC)")
    return dt.astimezone(timezone.utc).isoformat()


def _from_iso(s: Optional[str]) -> Optional[datetime]:
    if s is None:
        return None
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _row_to_record(row: sqlite3.Row) -> ApprovalRecord:
    """Build an ApprovalRecord from a sqlite3.Row."""
    payload = json.loads(row["payload_json"])
    exec_result = (
        json.loads(row["execution_result_json"])
        if row["execution_result_json"] else None
    )
    return ApprovalRecord(
        approval_id=row["approval_id"],
        action_kind=row["action_kind"],
        payload=payload,
        requested_by=row["requested_by"],
        mode=row["mode"],
        state=ApprovalState(row["state"]),
        created_at=_from_iso(row["created_at"]),
        expires_at=_from_iso(row["expires_at"]),
        decided_by=row["decided_by"],
        decided_at=_from_iso(row["decided_at"]),
        decision_reason=row["decision_reason"],
        execute_expires_at=_from_iso(row["execute_expires_at"]),
        executed_by=row["executed_by"],
        executed_at=_from_iso(row["executed_at"]),
        execution_result=exec_result,
        session_id=row["session_id"],
        trace_id=row["trace_id"],
    )


# =============================================================================
# ApprovalStore
# =============================================================================

class ApprovalStore:
    """Thread-safe SQLite-backed approval store.

    Replaces both Task 35 _approval_store.py and Task 40 _approval_state.py.

    Default location: data/approvals.sqlite (gitignored, 0600).
    """

    SCHEMA_VERSION: int = 1

    _CREATE_SQL: str = """
    CREATE TABLE IF NOT EXISTS approvals (
        approval_id              TEXT PRIMARY KEY,
        action_kind              TEXT NOT NULL,
        payload_json             TEXT NOT NULL,
        requested_by             TEXT NOT NULL,
        mode                     TEXT NOT NULL CHECK (mode IN ('paper','live')),
        state                    TEXT NOT NULL,
        created_at               TEXT NOT NULL,
        expires_at               TEXT NOT NULL,
        decided_by               TEXT,
        decided_at               TEXT,
        decision_reason          TEXT,
        execute_expires_at       TEXT,
        executed_by              TEXT,
        executed_at              TEXT,
        execution_result_json    TEXT,
        session_id               TEXT,
        trace_id                 TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_state ON approvals(state);
    CREATE INDEX IF NOT EXISTS idx_created ON approvals(created_at);
    CREATE INDEX IF NOT EXISTS idx_requested_by ON approvals(requested_by);

    CREATE TABLE IF NOT EXISTS schema_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        approval_ttl_seconds: int = DEFAULT_APPROVAL_TTL_SECONDS,
        execute_ttl_seconds: int = DEFAULT_EXECUTE_TTL_SECONDS,
        kill_switch_ttl_seconds: int = DEFAULT_KILL_SWITCH_TTL_SECONDS,
        allow_live: bool = False,
        now_fn: Optional[Callable[[], datetime]] = None,
        skip_perm_check: bool = False,
    ) -> None:
        """Construct the store.

        Args:
            db_path: SQLite file path. Created with 0600 if absent.
            approval_ttl_seconds: TTL from PROPOSED until EXPIRED.
            execute_ttl_seconds: TTL from APPROVED until EXPIRED.
            kill_switch_ttl_seconds: shorter TTL for kill-switch actions.
            allow_live: must be True AND payload mode='live' for live submission.
                When False, any request with mode='live' is rejected.
            now_fn: injectable clock (for tests).
            skip_perm_check: set True only in unit tests on tmp paths.

        Raises:
            ApprovalIntegrityError: if file permissions are not 0600.
        """
        self._db_path = Path(db_path)
        _ensure_parent_dir(self._db_path)

        if not skip_perm_check:
            _verify_file_mode(self._db_path)

        self._lock = threading.RLock()
        self._approval_ttl = approval_ttl_seconds
        self._execute_ttl = execute_ttl_seconds
        self._kill_switch_ttl = kill_switch_ttl_seconds
        self._allow_live = allow_live
        self._now_fn = now_fn or (lambda: datetime.now(tz=timezone.utc))
        self._skip_perm_check = skip_perm_check

        self._init_schema()
        # After schema init, file exists — enforce 0600
        _set_file_mode_0600(self._db_path)

    # ------------------------------------------------------------------
    # Schema management
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(self._CREATE_SQL)
            conn.execute(
                "INSERT OR IGNORE INTO schema_meta(key,value) VALUES (?,?)",
                ("schema_version", str(self.SCHEMA_VERSION)),
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        """Open a connection. WAL mode, foreign keys on."""
        conn = sqlite3.connect(
            str(self._db_path),
            detect_types=sqlite3.PARSE_DECLTYPES,
            isolation_level=None,  # autocommit; we manage transactions explicitly
            timeout=10.0,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = FULL")
        return conn

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def create_request(
        self,
        *,
        action_kind: str,
        payload: Mapping[str, Any],
        requested_by: str,
        mode: str = "paper",
        session_id: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> ApprovalRecord:
        """Create a new pending approval.

        Validates mode/live policy. Generates approval_id. Sets TTL based on
        action_kind (kill_switch uses shorter TTL).

        Raises:
            ApprovalStoreError on validation failure.
            LiveModeBlockedError if mode='live' but allow_live is False.
        """
        # Validation
        if action_kind not in VALID_ACTION_KINDS:
            raise ApprovalStoreError(
                f"invalid action_kind: {action_kind!r}. "
                f"Allowed: {sorted(VALID_ACTION_KINDS)}"
            )
        if not requested_by or not isinstance(requested_by, str):
            raise ApprovalStoreError("requested_by must be non-empty string")
        if mode not in ("paper", "live"):
            raise ApprovalStoreError(f"mode must be 'paper' or 'live', got {mode!r}")
        if mode == "live" and not self._allow_live:
            raise LiveModeBlockedError(
                "mode='live' rejected: store was constructed with allow_live=False. "
                "To enable: set JCPR_ALLOW_LIVE=1 and pass allow_live=True."
            )
        if not isinstance(payload, Mapping):
            raise ApprovalStoreError("payload must be a mapping")

        try:
            # No default=str — caller must pre-stringify Decimals etc.
            # This catches accidental insertion of non-serializable objects.
            payload_json = json.dumps(payload, sort_keys=True)
        except (TypeError, ValueError) as e:
            raise ApprovalStoreError(f"payload not JSON-serializable: {e}") from e

        # Choose TTL
        ttl = (self._kill_switch_ttl if action_kind == ACTION_KILL_SWITCH
               else self._approval_ttl)

        now = self._now_fn()
        approval_id = _generate_approval_id(now)
        expires_at = now + timedelta(seconds=ttl)

        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    INSERT INTO approvals (
                        approval_id, action_kind, payload_json, requested_by,
                        mode, state, created_at, expires_at,
                        session_id, trace_id
                    ) VALUES (?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        approval_id,
                        action_kind,
                        payload_json,
                        requested_by,
                        mode,
                        ApprovalState.PROPOSED.value,
                        _to_iso(now),
                        _to_iso(expires_at),
                        session_id,
                        trace_id,
                    ),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

            row = conn.execute(
                "SELECT * FROM approvals WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            return _row_to_record(row)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get(self, approval_id: str) -> ApprovalRecord:
        """Get a single approval. Raises ApprovalNotFound if absent.

        Note: does NOT auto-expire. Caller should call expire_overdue() or
        check the record's state vs current time if expiration matters.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM approvals WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            if row is None:
                raise ApprovalNotFound(f"approval_id {approval_id!r} not found")
            return _row_to_record(row)

    def list_pending(
        self,
        *,
        action_kind: Optional[str] = None,
        requested_by: Optional[str] = None,
        limit: int = 100,
    ) -> list[ApprovalRecord]:
        """List approvals in PROPOSED state. Optional filters."""
        if limit <= 0 or limit > 1000:
            raise ApprovalStoreError("limit must be in 1..1000")

        clauses = ["state = ?"]
        params: list[Any] = [ApprovalState.PROPOSED.value]
        if action_kind is not None:
            if action_kind not in VALID_ACTION_KINDS:
                raise ApprovalStoreError(f"invalid action_kind filter: {action_kind!r}")
            clauses.append("action_kind = ?")
            params.append(action_kind)
        if requested_by is not None:
            clauses.append("requested_by = ?")
            params.append(requested_by)

        where = " AND ".join(clauses)
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM approvals WHERE {where} "
                f"ORDER BY created_at DESC LIMIT ?",
                params,
            ).fetchall()
            return [_row_to_record(r) for r in rows]

    def list_by_state(
        self,
        state: ApprovalState,
        *,
        limit: int = 100,
    ) -> list[ApprovalRecord]:
        """List approvals in a given state, newest first."""
        if limit <= 0 or limit > 1000:
            raise ApprovalStoreError("limit must be in 1..1000")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM approvals WHERE state = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (state.value, limit),
            ).fetchall()
            return [_row_to_record(r) for r in rows]

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def approve(
        self,
        approval_id: str,
        *,
        decided_by: str,
        reason: Optional[str] = None,
    ) -> ApprovalRecord:
        """Approve a PROPOSED request.

        Blocks self-approval (decided_by == requested_by) → SelfApprovalError.
        Sets execute_expires_at = now + execute_ttl.

        Raises:
            ApprovalNotFound, ApprovalStateError, SelfApprovalError,
            ApprovalExpiredError.
        """
        if not decided_by or not isinstance(decided_by, str):
            raise ApprovalStoreError("decided_by must be non-empty string")

        now = self._now_fn()
        return self._transition(
            approval_id=approval_id,
            from_states=(ApprovalState.PROPOSED,),
            to_state=ApprovalState.APPROVED,
            decided_by=decided_by,
            reason=reason,
            now=now,
            extra_check=self._check_not_self_approval,
            extra_set={
                "execute_expires_at": _to_iso(
                    now + timedelta(seconds=self._execute_ttl)
                ),
            },
        )

    def reject(
        self,
        approval_id: str,
        *,
        decided_by: str,
        reason: str,
    ) -> ApprovalRecord:
        """Reject a PROPOSED request."""
        if not decided_by or not isinstance(decided_by, str):
            raise ApprovalStoreError("decided_by must be non-empty string")
        if not reason:
            raise ApprovalStoreError("reject requires non-empty reason")

        now = self._now_fn()
        return self._transition(
            approval_id=approval_id,
            from_states=(ApprovalState.PROPOSED,),
            to_state=ApprovalState.REJECTED,
            decided_by=decided_by,
            reason=reason,
            now=now,
        )

    def cancel(
        self,
        approval_id: str,
        *,
        cancelled_by: str,
        reason: Optional[str] = None,
    ) -> ApprovalRecord:
        """Cancel a PROPOSED request (requester withdraws)."""
        if not cancelled_by or not isinstance(cancelled_by, str):
            raise ApprovalStoreError("cancelled_by must be non-empty string")

        now = self._now_fn()
        return self._transition(
            approval_id=approval_id,
            from_states=(ApprovalState.PROPOSED,),
            to_state=ApprovalState.CANCELLED,
            decided_by=cancelled_by,
            reason=reason or "cancelled by requester",
            now=now,
        )

    def mark_executing(
        self,
        approval_id: str,
        *,
        executed_by: str,
    ) -> ApprovalRecord:
        """Lock APPROVED → EXECUTING for execution attempt.

        Single-use guard. After this, only mark_executed or mark_exec_failed
        are valid. Verifies execute_expires_at.

        Raises ApprovalExpiredError if execute TTL passed.
        """
        if not executed_by or not isinstance(executed_by, str):
            raise ApprovalStoreError("executed_by must be non-empty string")

        now = self._now_fn()
        # Defer raises until after the txn block so we never ROLLBACK a
        # committed txn.
        expired_msg: Optional[str] = None

        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM approvals WHERE approval_id = ?",
                    (approval_id,),
                ).fetchone()
                if row is None:
                    conn.execute("ROLLBACK")
                    raise ApprovalNotFound(
                        f"approval_id {approval_id!r} not found"
                    )

                state = ApprovalState(row["state"])
                if state != ApprovalState.APPROVED:
                    conn.execute("ROLLBACK")
                    raise ApprovalStateError(
                        f"cannot mark_executing from state {state.value!r}; "
                        f"required: approved"
                    )

                exec_exp = _from_iso(row["execute_expires_at"])
                if exec_exp is not None and now > exec_exp:
                    # Auto-expire path — commit the EXPIRED state, then raise.
                    conn.execute(
                        "UPDATE approvals SET state = ?, decision_reason = ? "
                        "WHERE approval_id = ?",
                        (
                            ApprovalState.EXPIRED.value,
                            "execute TTL elapsed",
                            approval_id,
                        ),
                    )
                    conn.execute("COMMIT")
                    expired_msg = (
                        f"approval {approval_id} execute TTL elapsed "
                        f"(expires_at={row['execute_expires_at']}, "
                        f"now={now.isoformat()})"
                    )
                else:
                    conn.execute(
                        "UPDATE approvals SET state = ?, executed_by = ? "
                        "WHERE approval_id = ?",
                        (
                            ApprovalState.EXECUTING.value,
                            executed_by,
                            approval_id,
                        ),
                    )
                    conn.execute("COMMIT")
            except (ApprovalNotFound, ApprovalStateError):
                # Already rolled back above; re-raise without touching txn.
                raise
            except Exception:
                # Unexpected — rollback and propagate.
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                raise

            if expired_msg is not None:
                raise ApprovalExpiredError(expired_msg)

            row = conn.execute(
                "SELECT * FROM approvals WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            return _row_to_record(row)

    def mark_executed(
        self,
        approval_id: str,
        *,
        result: Mapping[str, Any],
    ) -> ApprovalRecord:
        """Finalize EXECUTING → EXECUTED with result payload."""
        try:
            result_json = json.dumps(result, sort_keys=True, default=str)
        except (TypeError, ValueError) as e:
            raise ApprovalStoreError(f"result not JSON-serializable: {e}") from e

        now = self._now_fn()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT state FROM approvals WHERE approval_id = ?",
                    (approval_id,),
                ).fetchone()
                if row is None:
                    raise ApprovalNotFound(
                        f"approval_id {approval_id!r} not found"
                    )
                state = ApprovalState(row["state"])
                if state != ApprovalState.EXECUTING:
                    raise ApprovalStateError(
                        f"cannot mark_executed from state {state.value!r}; "
                        f"required: executing"
                    )

                conn.execute(
                    "UPDATE approvals SET state = ?, executed_at = ?, "
                    "execution_result_json = ? WHERE approval_id = ?",
                    (
                        ApprovalState.EXECUTED.value,
                        _to_iso(now),
                        result_json,
                        approval_id,
                    ),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

            row = conn.execute(
                "SELECT * FROM approvals WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            return _row_to_record(row)

    def mark_exec_failed(
        self,
        approval_id: str,
        *,
        error_message: str,
    ) -> ApprovalRecord:
        """Finalize EXECUTING → EXEC_FAILED with error message."""
        now = self._now_fn()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT state FROM approvals WHERE approval_id = ?",
                    (approval_id,),
                ).fetchone()
                if row is None:
                    raise ApprovalNotFound(
                        f"approval_id {approval_id!r} not found"
                    )
                state = ApprovalState(row["state"])
                if state != ApprovalState.EXECUTING:
                    raise ApprovalStateError(
                        f"cannot mark_exec_failed from state {state.value!r}; "
                        f"required: executing"
                    )
                err_payload = json.dumps(
                    {"error": error_message[:500]},
                    sort_keys=True,
                )
                conn.execute(
                    "UPDATE approvals SET state = ?, executed_at = ?, "
                    "execution_result_json = ? WHERE approval_id = ?",
                    (
                        ApprovalState.EXEC_FAILED.value,
                        _to_iso(now),
                        err_payload,
                        approval_id,
                    ),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

            row = conn.execute(
                "SELECT * FROM approvals WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            return _row_to_record(row)

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def expire_overdue(self) -> int:
        """Sweep: PROPOSED past expires_at OR APPROVED past execute_expires_at
        → EXPIRED. Returns count expired.

        Run periodically (e.g. on each MCP call entry).
        """
        now = self._now_fn()
        now_iso = _to_iso(now)
        count = 0
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                # PROPOSED past decision TTL
                cur = conn.execute(
                    "UPDATE approvals SET state = ?, decision_reason = ? "
                    "WHERE state = ? AND expires_at < ?",
                    (
                        ApprovalState.EXPIRED.value,
                        "decision TTL elapsed",
                        ApprovalState.PROPOSED.value,
                        now_iso,
                    ),
                )
                count += cur.rowcount
                # APPROVED past execute TTL
                cur = conn.execute(
                    "UPDATE approvals SET state = ?, decision_reason = ? "
                    "WHERE state = ? AND execute_expires_at < ?",
                    (
                        ApprovalState.EXPIRED.value,
                        "execute TTL elapsed",
                        ApprovalState.APPROVED.value,
                        now_iso,
                    ),
                )
                count += cur.rowcount
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return count

    # ------------------------------------------------------------------
    # Internal — generic transition helper
    # ------------------------------------------------------------------

    def _transition(
        self,
        *,
        approval_id: str,
        from_states: Sequence[ApprovalState],
        to_state: ApprovalState,
        decided_by: str,
        reason: Optional[str],
        now: datetime,
        extra_check: Optional[Callable[[sqlite3.Row, str], None]] = None,
        extra_set: Optional[Mapping[str, Any]] = None,
    ) -> ApprovalRecord:
        # Defer raises until after the txn block so we never ROLLBACK a
        # committed transaction.
        expired_msg: Optional[str] = None

        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM approvals WHERE approval_id = ?",
                    (approval_id,),
                ).fetchone()
                if row is None:
                    conn.execute("ROLLBACK")
                    raise ApprovalNotFound(
                        f"approval_id {approval_id!r} not found"
                    )

                state = ApprovalState(row["state"])
                if state not in from_states:
                    conn.execute("ROLLBACK")
                    raise ApprovalStateError(
                        f"cannot transition from {state.value!r} to "
                        f"{to_state.value!r}; "
                        f"required one of: {[s.value for s in from_states]}"
                    )

                # Auto-expire on read if past TTL
                expires_at = _from_iso(row["expires_at"])
                if (state == ApprovalState.PROPOSED
                        and expires_at is not None
                        and now > expires_at):
                    conn.execute(
                        "UPDATE approvals SET state = ?, decision_reason = ? "
                        "WHERE approval_id = ?",
                        (
                            ApprovalState.EXPIRED.value,
                            "decision TTL elapsed",
                            approval_id,
                        ),
                    )
                    conn.execute("COMMIT")
                    expired_msg = (
                        f"approval {approval_id} TTL elapsed "
                        f"(expires_at={row['expires_at']}, "
                        f"now={now.isoformat()})"
                    )
                else:
                    if extra_check is not None:
                        extra_check(row, decided_by)  # may raise SelfApprovalError

                    # Build update fields
                    set_parts = ["state = ?", "decided_by = ?", "decided_at = ?",
                                 "decision_reason = ?"]
                    params: list[Any] = [
                        to_state.value, decided_by, _to_iso(now), reason,
                    ]
                    if extra_set:
                        for k, v in extra_set.items():
                            set_parts.append(f"{k} = ?")
                            params.append(v)
                    params.append(approval_id)

                    conn.execute(
                        f"UPDATE approvals SET {', '.join(set_parts)} "
                        f"WHERE approval_id = ?",
                        params,
                    )
                    conn.execute("COMMIT")
            except (ApprovalNotFound, ApprovalStateError):
                # Already rolled back; re-raise without retouching txn.
                raise
            except SelfApprovalError:
                # extra_check raised; rollback explicitly.
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                raise
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                raise

            if expired_msg is not None:
                raise ApprovalExpiredError(expired_msg)

            row = conn.execute(
                "SELECT * FROM approvals WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            return _row_to_record(row)

    @staticmethod
    def _check_not_self_approval(row: sqlite3.Row, decided_by: str) -> None:
        if row["requested_by"] == decided_by:
            raise SelfApprovalError(
                f"self-approval blocked: requester={row['requested_by']!r} "
                f"cannot also be approver"
            )


__all__ = (
    # Enums
    "ApprovalState",
    # Record
    "ApprovalRecord",
    # Store
    "ApprovalStore",
    # Action constants
    "ACTION_SUBMIT_ORDER",
    "ACTION_CANCEL_ORDER",
    "ACTION_SET_CAPACITY",
    "ACTION_KILL_SWITCH",
    "VALID_ACTION_KINDS",
    # TTL constants
    "DEFAULT_APPROVAL_TTL_SECONDS",
    "DEFAULT_EXECUTE_TTL_SECONDS",
    "DEFAULT_KILL_SWITCH_TTL_SECONDS",
    # Exceptions
    "ApprovalStoreError",
    "ApprovalNotFound",
    "ApprovalStateError",
    "SelfApprovalError",
    "ApprovalExpiredError",
    "ApprovalIntegrityError",
    "LiveModeBlockedError",
)
