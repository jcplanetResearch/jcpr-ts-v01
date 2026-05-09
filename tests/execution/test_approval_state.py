"""Tests for execution/_approval_state.py — state machine + persistence."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

    ALLOWED_TRANSITIONS,
    DEFAULT_EXPIRY_SEC,
    MAX_PAYLOAD_BYTES,
    ApprovalProposal,
    ApprovalState,
    ApprovalStore,
    ProposalNotFoundError,
    SelfApprovalError,
    StateTransitionError,
)


# =============================================================================
# ApprovalProposal frozen dataclass
# =============================================================================

@pytest.fixture
def utc_now() -> datetime:
    return datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)


class TestApprovalProposal:
    def test_accepts_valid(self, utc_now):
        p = ApprovalProposal(
            approval_id="ap-test-1",
            action_type="place_order",
            payload={"symbol": "005930", "qty": "10"},
            requested_by="agent",
            proposed_at_utc=utc_now,
            expires_at_utc=utc_now + timedelta(minutes=5),
            state=ApprovalState.PROPOSED,
        )
        assert p.state == ApprovalState.PROPOSED
        assert not p.is_terminal
        assert not p.is_executable

    def test_executable_when_approved(self, utc_now):
        p = ApprovalProposal(
            approval_id="ap-1",
            action_type="place_order",
            payload={},
            requested_by="agent",
            proposed_at_utc=utc_now,
            expires_at_utc=utc_now + timedelta(minutes=5),
            state=ApprovalState.APPROVED,
            decided_by="operator",
            decided_at_utc=utc_now + timedelta(seconds=30),
        )
        assert p.is_executable
        assert not p.is_terminal

    def test_terminal_states(self, utc_now):
        for state in (ApprovalState.EXECUTED, ApprovalState.REJECTED,
                      ApprovalState.EXPIRED, ApprovalState.CANCELLED):
            kwargs = dict(
                approval_id="ap-1",
                action_type="x",
                payload={},
                requested_by="agent",
                proposed_at_utc=utc_now,
                expires_at_utc=utc_now + timedelta(minutes=5),
                state=state,
            )
            if state == ApprovalState.REJECTED:
                kwargs.update(decided_by="operator",
                              decided_at_utc=utc_now + timedelta(seconds=10))
            p = ApprovalProposal(**kwargs)
            assert p.is_terminal

    def test_rejects_self_approval(self, utc_now):
        with pytest.raises(SelfApprovalError, match="self-approval"):
            ApprovalProposal(
                approval_id="ap-1",
                action_type="x",
                payload={},
                requested_by="alice",
                proposed_at_utc=utc_now,
                expires_at_utc=utc_now + timedelta(minutes=5),
                state=ApprovalState.APPROVED,
                decided_by="alice",  # same as requested_by
                decided_at_utc=utc_now + timedelta(seconds=10),
            )

    def test_rejects_expires_before_proposed(self, utc_now):
        with pytest.raises(ValueError, match="expires_at_utc must be after"):
            ApprovalProposal(
                approval_id="ap-1",
                action_type="x",
                payload={},
                requested_by="a",
                proposed_at_utc=utc_now,
                expires_at_utc=utc_now,  # equal — invalid
                state=ApprovalState.PROPOSED,
            )

    def test_rejects_naive_datetime(self):
        with pytest.raises(ValueError, match="tz-aware"):
            ApprovalProposal(
                approval_id="ap-1",
                action_type="x",
                payload={},
                requested_by="a",
                proposed_at_utc=datetime(2026, 5, 7),
                expires_at_utc=datetime(2026, 5, 7, 12),
                state=ApprovalState.PROPOSED,
            )

    def test_rejects_too_long_id(self, utc_now):
        with pytest.raises(ValueError, match="approval_id"):
            ApprovalProposal(
                approval_id="x" * 100,
                action_type="x",
                payload={},
                requested_by="a",
                proposed_at_utc=utc_now,
                expires_at_utc=utc_now + timedelta(minutes=5),
                state=ApprovalState.PROPOSED,
            )

    def test_rejects_oversized_payload(self, utc_now):
        big = {"data": "x" * (MAX_PAYLOAD_BYTES + 100)}
        with pytest.raises(ValueError, match="exceeds"):
            ApprovalProposal(
                approval_id="ap-1",
                action_type="x",
                payload=big,
                requested_by="a",
                proposed_at_utc=utc_now,
                expires_at_utc=utc_now + timedelta(minutes=5),
                state=ApprovalState.PROPOSED,
            )


# =============================================================================
# ALLOWED_TRANSITIONS
# =============================================================================

class TestTransitionMatrix:
    def test_proposed_can_transition_to_decisions(self):
        allowed = ALLOWED_TRANSITIONS[ApprovalState.PROPOSED]
        assert ApprovalState.APPROVED in allowed
        assert ApprovalState.REJECTED in allowed
        assert ApprovalState.EXPIRED in allowed
        assert ApprovalState.CANCELLED in allowed
        # Cannot skip approval phase
        assert ApprovalState.EXECUTED not in allowed

    def test_approved_can_only_execute_or_terminal(self):
        allowed = ALLOWED_TRANSITIONS[ApprovalState.APPROVED]
        assert ApprovalState.EXECUTED in allowed
        # Cannot go back to PROPOSED
        assert ApprovalState.PROPOSED not in allowed
        assert ApprovalState.REJECTED not in allowed

    def test_terminal_states_have_no_transitions(self):
        for state in (ApprovalState.EXECUTED, ApprovalState.REJECTED,
                      ApprovalState.EXPIRED, ApprovalState.CANCELLED):
            assert ALLOWED_TRANSITIONS[state] == frozenset()


# =============================================================================
# ApprovalStore — SQLite persistence
# =============================================================================

class TestApprovalStore:
    @pytest.fixture
    def store(self, tmp_path, utc_now):
        # Mutable clock for time-travel tests
        self._clock = [utc_now]
        s = ApprovalStore(
            db_path=tmp_path / "test_approvals.db",
            _now_fn=lambda: self._clock[0],
        )
        return s

    def test_proposes_with_unique_ids(self, store):
        p1 = store.propose(action_type="place_order", payload={"x": 1},
                           requested_by="agent")
        p2 = store.propose(action_type="place_order", payload={"x": 2},
                           requested_by="agent")
        assert p1.approval_id != p2.approval_id
        assert p1.approval_id.startswith("ap-")

    def test_get_returns_proposal(self, store):
        p = store.propose(action_type="x", payload={}, requested_by="agent")
        retrieved = store.get(p.approval_id)
        assert retrieved.approval_id == p.approval_id
        assert retrieved.state == ApprovalState.PROPOSED

    def test_get_missing_raises(self, store):
        with pytest.raises(ProposalNotFoundError):
            store.get("nonexistent")

    def test_list_pending(self, store):
        p1 = store.propose(action_type="x", payload={}, requested_by="a")
        p2 = store.propose(action_type="y", payload={}, requested_by="b")
        # Approve p1 → still pending list (approved + proposed)
        store.transition(approval_id=p1.approval_id,
                         target_state=ApprovalState.APPROVED, actor="op1")
        # Reject p2 → terminal, not in pending
        p3 = store.propose(action_type="z", payload={}, requested_by="c")
        store.transition(approval_id=p3.approval_id,
                         target_state=ApprovalState.REJECTED,
                         actor="op1", reason_kr="too risky")
        pending = store.list_pending()
        ids = {p.approval_id for p in pending}
        assert p1.approval_id in ids
        assert p2.approval_id in ids
        assert p3.approval_id not in ids

    def test_persistence_across_instances(self, tmp_path, utc_now):
        db = tmp_path / "persist.db"
        s1 = ApprovalStore(db_path=db, _now_fn=lambda: utc_now)
        p = s1.propose(action_type="x", payload={"key": "value"},
                       requested_by="agent")
        # New instance — should see the proposal
        s2 = ApprovalStore(db_path=db, _now_fn=lambda: utc_now)
        retrieved = s2.get(p.approval_id)
        assert retrieved.approval_id == p.approval_id
        assert retrieved.payload == {"key": "value"}

    def test_db_file_has_0600_perms(self, tmp_path, utc_now):
        if os.name != "posix":
            pytest.skip("POSIX-only")
        db = tmp_path / "perms.db"
        ApprovalStore(db_path=db, _now_fn=lambda: utc_now)
        mode = db.stat().st_mode & 0o777
        # Allow at least 0600; exact mode varies by umask but no group/other
        assert mode & 0o077 == 0


class TestStateTransitions:
    @pytest.fixture
    def store(self, tmp_path, utc_now):
        self._clock = [utc_now]
        return ApprovalStore(
            db_path=tmp_path / "trans.db",
            _now_fn=lambda: self._clock[0],
        )

    def test_proposed_to_approved(self, store):
        p = store.propose(action_type="x", payload={}, requested_by="agent")
        result = store.transition(
            approval_id=p.approval_id,
            target_state=ApprovalState.APPROVED,
            actor="operator",
        )
        assert result.state == ApprovalState.APPROVED
        assert result.decided_by == "operator"
        assert result.decided_at_utc is not None

    def test_proposed_to_rejected_with_reason(self, store):
        p = store.propose(action_type="x", payload={}, requested_by="agent")
        result = store.transition(
            approval_id=p.approval_id,
            target_state=ApprovalState.REJECTED,
            actor="operator",
            reason_kr="위험도 초과",
        )
        assert result.state == ApprovalState.REJECTED
        assert result.decision_reason_kr == "위험도 초과"

    def test_approved_to_executed(self, store):
        p = store.propose(action_type="x", payload={}, requested_by="agent")
        store.transition(
            approval_id=p.approval_id,
            target_state=ApprovalState.APPROVED,
            actor="operator",
        )
        result = store.transition(
            approval_id=p.approval_id,
            target_state=ApprovalState.EXECUTED,
            execution_result={"success": True, "broker_order_id": "ord-1"},
        )
        assert result.state == ApprovalState.EXECUTED
        assert result.execution_result == {"success": True,
                                            "broker_order_id": "ord-1"}

    def test_cannot_skip_approval(self, store):
        p = store.propose(action_type="x", payload={}, requested_by="agent")
        with pytest.raises(StateTransitionError, match="cannot transition"):
            store.transition(
                approval_id=p.approval_id,
                target_state=ApprovalState.EXECUTED,
            )

    def test_cannot_transition_terminal(self, store):
        p = store.propose(action_type="x", payload={}, requested_by="agent")
        store.transition(
            approval_id=p.approval_id,
            target_state=ApprovalState.REJECTED,
            actor="operator",
            reason_kr="x",
        )
        with pytest.raises(StateTransitionError, match="cannot transition"):
            store.transition(
                approval_id=p.approval_id,
                target_state=ApprovalState.APPROVED,
                actor="operator2",
            )

    def test_self_approval_blocked(self, store):
        p = store.propose(action_type="x", payload={}, requested_by="alice")
        with pytest.raises(SelfApprovalError, match="self-approval"):
            store.transition(
                approval_id=p.approval_id,
                target_state=ApprovalState.APPROVED,
                actor="alice",  # same as requested_by
            )

    def test_self_rejection_also_blocked(self, store):
        p = store.propose(action_type="x", payload={}, requested_by="alice")
        with pytest.raises(SelfApprovalError):
            store.transition(
                approval_id=p.approval_id,
                target_state=ApprovalState.REJECTED,
                actor="alice",
                reason_kr="x",
            )

    def test_decision_requires_actor(self, store):
        p = store.propose(action_type="x", payload={}, requested_by="agent")
        with pytest.raises(ValueError, match="actor required"):
            store.transition(
                approval_id=p.approval_id,
                target_state=ApprovalState.APPROVED,
            )

    def test_auto_expires_stale_proposal(self, store, utc_now):
        p = store.propose(
            action_type="x", payload={}, requested_by="agent",
            expiry_sec=10,
        )
        # Time travel — past the expiry
        self._clock[0] = utc_now + timedelta(seconds=20)
        with pytest.raises(StateTransitionError, match="expired"):
            store.transition(
                approval_id=p.approval_id,
                target_state=ApprovalState.APPROVED,
                actor="operator",
            )
        # Verify it was auto-marked EXPIRED
        result = store.get(p.approval_id)
        assert result.state == ApprovalState.EXPIRED

    def test_explicit_expire_works(self, store):
        p = store.propose(action_type="x", payload={}, requested_by="agent")
        result = store.transition(
            approval_id=p.approval_id,
            target_state=ApprovalState.EXPIRED,
        )
        assert result.state == ApprovalState.EXPIRED

    def test_cancel_works(self, store):
        p = store.propose(action_type="x", payload={}, requested_by="agent")
        result = store.transition(
            approval_id=p.approval_id,
            target_state=ApprovalState.CANCELLED,
        )
        assert result.state == ApprovalState.CANCELLED


class TestProposeValidation:
    @pytest.fixture
    def store(self, tmp_path, utc_now):
        return ApprovalStore(
            db_path=tmp_path / "valid.db",
            _now_fn=lambda: utc_now,
        )

    def test_rejects_too_short_expiry(self, store):
        with pytest.raises(ValueError, match="expiry_sec"):
            store.propose(
                action_type="x", payload={}, requested_by="agent",
                expiry_sec=5,  # < 10
            )

    def test_rejects_too_long_expiry(self, store):
        with pytest.raises(ValueError, match="expiry_sec"):
            store.propose(
                action_type="x", payload={}, requested_by="agent",
                expiry_sec=4000,  # > 3600
            )
