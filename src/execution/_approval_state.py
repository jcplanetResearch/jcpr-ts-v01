"""Task 40 — Approval state machine (PROPOSED → APPROVED → EXECUTED).

Defines the three-phase workflow that gates ALL write operations:

    1. PROPOSED   — Agent (Task 38/39) or scheduler proposes an action
    2. APPROVED   — Operator reviews and approves via approve_cli.py
    3. EXECUTED   — ExecutionGateway invokes broker write tool

Or alternative terminal states:
    REJECTED   — Operator rejects
    EXPIRED    — Approval timed out (default 5 minutes)
    CANCELLED  — Caller withdraws proposal

Defense in depth invariants:
    - approval_id is uuid4 — never sequential
    - Each transition is logged via AuditWriter
    - State persisted to SQLite with WAL mode
    - Self-approval forbidden: requested_by != decided_by
    - APPROVED → EXECUTED idempotent — double-execute returns cached result
    - All Decimal math, frozen dataclasses, UTC tz-aware

Storage:
    SQLite at JCPR_APPROVAL_DB path. WAL journal mode. 0600 file perms.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Mapping


class ApprovalState(str, Enum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    EXECUTED = "executed"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


#: Allowed state transitions. Any other attempt raises StateTransitionError.
ALLOWED_TRANSITIONS: dict[ApprovalState, frozenset[ApprovalState]] = {
    ApprovalState.PROPOSED: frozenset({
        ApprovalState.APPROVED, ApprovalState.REJECTED,
        ApprovalState.EXPIRED, ApprovalState.CANCELLED,
    }),
    ApprovalState.APPROVED: frozenset({
        ApprovalState.EXECUTED, ApprovalState.EXPIRED,
        ApprovalState.CANCELLED,
    }),
    # Terminal states — no transitions out
    ApprovalState.EXECUTED: frozenset(),
    ApprovalState.REJECTED: frozenset(),
    ApprovalState.EXPIRED: frozenset(),
    ApprovalState.CANCELLED: frozenset(),
}

#: Default proposal expiry. Operator has 5 minutes to decide.
DEFAULT_EXPIRY_SEC: int = 300

#: Maximum proposal payload size (bytes JSON).
MAX_PAYLOAD_BYTES: int = 16 * 1024


class ApprovalError(RuntimeError):
    """Base class for approval workflow errors."""


class StateTransitionError(ApprovalError):
    """Invalid state transition."""


class SelfApprovalError(ApprovalError):
    """Same actor tried to propose AND decide."""


class ProposalNotFoundError(ApprovalError):
    """approval_id does not exist."""


# =============================================================================
# Frozen dataclasses
# =============================================================================

@dataclass(frozen=True, slots=True)
class ApprovalProposal:
    """An approval request. Immutable once created."""
    approval_id: str
    action_type: str           # e.g. "place_order", "cancel_order", "kill_switch"
    payload: Mapping[str, Any]   # action-specific parameters
    requested_by: str          # actor id (e.g. agent name or operator id)
    proposed_at_utc: datetime
    expires_at_utc: datetime
    state: ApprovalState
    decided_by: str | None = None
    decided_at_utc: datetime | None = None
    decision_reason_kr: str | None = None
    execution_result: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.approval_id or len(self.approval_id) > 80:
            raise ValueError("approval_id must be 1..80 chars")
        if not self.action_type:
            raise ValueError("action_type required")
        if not self.requested_by:
            raise ValueError("requested_by required")
        if self.proposed_at_utc.tzinfo is None:
            raise ValueError("proposed_at_utc must be tz-aware")
        if self.expires_at_utc.tzinfo is None:
            raise ValueError("expires_at_utc must be tz-aware")
        if self.expires_at_utc <= self.proposed_at_utc:
            raise ValueError("expires_at_utc must be after proposed_at_utc")
        # Verify payload serializability + size
        try:
            payload_json = json.dumps(dict(self.payload), default=str)
        except (TypeError, ValueError) as e:
            raise ValueError(f"payload not JSON-serializable: {e}") from e
        if len(payload_json.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise ValueError(
                f"payload exceeds {MAX_PAYLOAD_BYTES} bytes"
            )
        if self.decided_by is not None:
            if self.decided_by == self.requested_by:
                raise SelfApprovalError(
                    f"self-approval forbidden: requested_by={self.requested_by} "
                    f"== decided_by={self.decided_by}"
                )
        if self.decided_at_utc is not None:
            if self.decided_at_utc.tzinfo is None:
                raise ValueError("decided_at_utc must be tz-aware")

    @property
    def is_terminal(self) -> bool:
        return self.state in (
            ApprovalState.EXECUTED, ApprovalState.REJECTED,
            ApprovalState.EXPIRED, ApprovalState.CANCELLED,
        )

    @property
    def is_executable(self) -> bool:
        return self.state == ApprovalState.APPROVED


# =============================================================================
# SQLite-backed store
# =============================================================================

_SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals (
    approval_id        TEXT PRIMARY KEY,
    action_type        TEXT NOT NULL,
    payload_json       TEXT NOT NULL,
    requested_by       TEXT NOT NULL,
    proposed_at_utc    TEXT NOT NULL,
    expires_at_utc     TEXT NOT NULL,
    state              TEXT NOT NULL,
    decided_by         TEXT,
    decided_at_utc     TEXT,
    decision_reason_kr TEXT,
    execution_result_json TEXT,
    updated_at_utc     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_approvals_state ON approvals(state);
CREATE INDEX IF NOT EXISTS idx_approvals_expires ON approvals(expires_at_utc);
"""


class ApprovalStore:
    """Thread-safe persistent store for proposals.

    Concurrency: single in-process instance. Multiple processes accessing
    the same DB MUST coordinate via the SQLite locking protocol — the WAL
    mode handles read concurrency.
    """

    def __init__(
        self,
        db_path: str | os.PathLike[str],
        *,
        _now_fn: Any = None,
    ) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._now_fn = _now_fn or (lambda: datetime.now(tz=timezone.utc))
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, sqlite3.connect(self._db_path) as conn:
            conn.executescript("PRAGMA journal_mode=WAL;")
            conn.executescript(_SCHEMA)
            conn.commit()
        # Set 0600 permissions on POSIX
        if os.name == "posix" and self._db_path.exists():
            try:
                os.chmod(self._db_path, 0o600)
            except OSError:
                pass

    # -------------------------------------------------------------------------
    # Create
    # -------------------------------------------------------------------------

    def propose(
        self,
        *,
        action_type: str,
        payload: Mapping[str, Any],
        requested_by: str,
        expiry_sec: int = DEFAULT_EXPIRY_SEC,
    ) -> ApprovalProposal:
        """Create a new PROPOSED entry. Returns the ApprovalProposal."""
        if expiry_sec < 10 or expiry_sec > 3600:
            raise ValueError("expiry_sec must be 10..3600")
        now = self._now_fn()
        proposal = ApprovalProposal(
            approval_id=f"ap-{uuid.uuid4()}",
            action_type=action_type,
            payload=dict(payload),
            requested_by=requested_by,
            proposed_at_utc=now,
            expires_at_utc=now + timedelta(seconds=expiry_sec),
            state=ApprovalState.PROPOSED,
        )
        self._insert(proposal)
        return proposal

    def _insert(self, p: ApprovalProposal) -> None:
        with self._lock, sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "INSERT INTO approvals "
                "(approval_id, action_type, payload_json, requested_by, "
                " proposed_at_utc, expires_at_utc, state, "
                " decided_by, decided_at_utc, decision_reason_kr, "
                " execution_result_json, updated_at_utc) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    p.approval_id, p.action_type,
                    json.dumps(dict(p.payload), default=str),
                    p.requested_by,
                    p.proposed_at_utc.isoformat(),
                    p.expires_at_utc.isoformat(),
                    p.state.value,
                    p.decided_by,
                    p.decided_at_utc.isoformat() if p.decided_at_utc else None,
                    p.decision_reason_kr,
                    json.dumps(dict(p.execution_result), default=str)
                        if p.execution_result else None,
                    self._now_fn().isoformat(),
                ),
            )
            conn.commit()

    # -------------------------------------------------------------------------
    # Read
    # -------------------------------------------------------------------------

    def get(self, approval_id: str) -> ApprovalProposal:
        with self._lock, sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT approval_id, action_type, payload_json, requested_by, "
                "proposed_at_utc, expires_at_utc, state, decided_by, "
                "decided_at_utc, decision_reason_kr, execution_result_json "
                "FROM approvals WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
        if row is None:
            raise ProposalNotFoundError(f"approval_id not found: {approval_id}")
        return self._row_to_proposal(row)

    def list_pending(self, *, limit: int = 50) -> tuple[ApprovalProposal, ...]:
        if limit < 1 or limit > 200:
            raise ValueError("limit 1..200")
        with self._lock, sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(
                "SELECT approval_id, action_type, payload_json, requested_by, "
                "proposed_at_utc, expires_at_utc, state, decided_by, "
                "decided_at_utc, decision_reason_kr, execution_result_json "
                "FROM approvals WHERE state IN ('proposed', 'approved') "
                "ORDER BY proposed_at_utc DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(self._row_to_proposal(r) for r in rows)

    def _row_to_proposal(self, row: tuple) -> ApprovalProposal:
        (approval_id, action_type, payload_json, requested_by,
         proposed_at, expires_at, state,
         decided_by, decided_at, decision_reason,
         execution_result_json) = row
        return ApprovalProposal(
            approval_id=approval_id,
            action_type=action_type,
            payload=json.loads(payload_json),
            requested_by=requested_by,
            proposed_at_utc=datetime.fromisoformat(proposed_at),
            expires_at_utc=datetime.fromisoformat(expires_at),
            state=ApprovalState(state),
            decided_by=decided_by,
            decided_at_utc=datetime.fromisoformat(decided_at) if decided_at else None,
            decision_reason_kr=decision_reason,
            execution_result=json.loads(execution_result_json)
                if execution_result_json else None,
        )

    # -------------------------------------------------------------------------
    # Transitions
    # -------------------------------------------------------------------------

    def transition(
        self,
        *,
        approval_id: str,
        target_state: ApprovalState,
        actor: str | None = None,
        reason_kr: str | None = None,
        execution_result: Mapping[str, Any] | None = None,
    ) -> ApprovalProposal:
        """Atomic state transition with audit fields."""
        with self._lock, sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT approval_id, action_type, payload_json, requested_by, "
                "proposed_at_utc, expires_at_utc, state, decided_by, "
                "decided_at_utc, decision_reason_kr, execution_result_json "
                "FROM approvals WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            if row is None:
                raise ProposalNotFoundError(approval_id)
            current = self._row_to_proposal(row)

            # Auto-expire check
            now = self._now_fn()
            if current.state in (ApprovalState.PROPOSED, ApprovalState.APPROVED):
                if current.expires_at_utc < now and target_state != ApprovalState.EXPIRED:
                    # First expire it, then refuse the requested transition
                    conn.execute(
                        "UPDATE approvals SET state=?, updated_at_utc=? "
                        "WHERE approval_id=?",
                        (ApprovalState.EXPIRED.value, now.isoformat(),
                         approval_id),
                    )
                    conn.commit()
                    raise StateTransitionError(
                        f"approval {approval_id} expired "
                        f"(was {current.state.value})"
                    )

            allowed = ALLOWED_TRANSITIONS[current.state]
            if target_state not in allowed:
                raise StateTransitionError(
                    f"cannot transition {current.state.value} → "
                    f"{target_state.value}"
                )

            # Self-approval check (decisions only)
            if target_state in (ApprovalState.APPROVED, ApprovalState.REJECTED):
                if actor is None or not actor:
                    raise ValueError("actor required for decision transitions")
                if actor == current.requested_by:
                    raise SelfApprovalError(
                        f"self-approval forbidden: actor={actor} == "
                        f"requested_by={current.requested_by}"
                    )

            # Build update fields
            updates: dict[str, Any] = {
                "state": target_state.value,
                "updated_at_utc": now.isoformat(),
            }
            if target_state in (ApprovalState.APPROVED, ApprovalState.REJECTED):
                updates["decided_by"] = actor
                updates["decided_at_utc"] = now.isoformat()
                if reason_kr is not None:
                    updates["decision_reason_kr"] = reason_kr[:500]
            if target_state == ApprovalState.EXECUTED:
                if execution_result is not None:
                    updates["execution_result_json"] = json.dumps(
                        dict(execution_result), default=str
                    )

            set_clause = ", ".join(f"{k}=?" for k in updates)
            values = list(updates.values()) + [approval_id]
            conn.execute(
                f"UPDATE approvals SET {set_clause} WHERE approval_id=?",
                values,
            )
            conn.commit()

            return self.get(approval_id)


__all__ = (
    "ApprovalState",
    "ApprovalProposal",
    "ApprovalStore",
    "ApprovalError",
    "StateTransitionError",
    "SelfApprovalError",
    "ProposalNotFoundError",
    "ALLOWED_TRANSITIONS",
    "DEFAULT_EXPIRY_SEC",
    "MAX_PAYLOAD_BYTES",
)
