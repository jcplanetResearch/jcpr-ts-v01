"""Tests for unified ApprovalStore (Phase 1 integration).

Coverage targets:
    - 3-phase + cancellation lifecycle
    - All state transitions (valid + invalid)
    - Self-approval blocking
    - TTL expiration (decision + execute)
    - Live mode policy
    - Concurrent access (thread safety)
    - File permission enforcement
    - JSON serialization edge cases
    - approval_id format guarantees
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

# Path setup
_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
sys.path.insert(0, str(_REPO))

from src.execution.approval_store import (  # noqa: E402
    ACTION_CANCEL_ORDER,
    ACTION_KILL_SWITCH,
    ACTION_SET_CAPACITY,
    ACTION_SUBMIT_ORDER,
    DEFAULT_APPROVAL_TTL_SECONDS,
    DEFAULT_EXECUTE_TTL_SECONDS,
    DEFAULT_KILL_SWITCH_TTL_SECONDS,
    ApprovalExpiredError,
    ApprovalIntegrityError,
    ApprovalNotFound,
    ApprovalRecord,
    ApprovalState,
    ApprovalStateError,
    ApprovalStore,
    ApprovalStoreError,
    LiveModeBlockedError,
    SelfApprovalError,
)


# =============================================================================
# Fixtures
# =============================================================================

class _FakeClock:
    """Manually-advanced clock for deterministic TTL tests."""

    def __init__(self, start: datetime) -> None:
        self._t = start

    def now(self) -> datetime:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t = self._t + timedelta(seconds=seconds)


@pytest.fixture
def clock() -> _FakeClock:
    return _FakeClock(datetime(2026, 5, 7, 9, 0, 0, tzinfo=timezone.utc))


@pytest.fixture
def store(tmp_path, clock) -> ApprovalStore:
    db = tmp_path / "approvals.sqlite"
    return ApprovalStore(
        db_path=db,
        now_fn=clock.now,
        skip_perm_check=True,  # tmp_path has different mode on some CI
    )


@pytest.fixture
def live_store(tmp_path, clock) -> ApprovalStore:
    db = tmp_path / "approvals_live.sqlite"
    return ApprovalStore(
        db_path=db,
        now_fn=clock.now,
        allow_live=True,
        skip_perm_check=True,
    )


def _basic_payload() -> dict:
    return {
        "symbol": "005930",
        "side": "buy",
        "qty": 10,
        "order_type": "limit",
        "price_krw": "70000",
        "strategy_id": "momentum_v1",
    }


# =============================================================================
# Construction & schema
# =============================================================================

class TestConstruction:

    def test_creates_db_file(self, tmp_path, clock):
        db = tmp_path / "sub" / "approvals.sqlite"
        assert not db.exists()
        ApprovalStore(db_path=db, now_fn=clock.now, skip_perm_check=True)
        assert db.exists()

    def test_creates_parent_directory(self, tmp_path, clock):
        db = tmp_path / "deeply" / "nested" / "approvals.sqlite"
        ApprovalStore(db_path=db, now_fn=clock.now, skip_perm_check=True)
        assert db.parent.exists()

    @pytest.mark.skipif(os.name != "posix", reason="POSIX-only")
    def test_file_mode_is_0600_after_init(self, tmp_path, clock):
        db = tmp_path / "approvals.sqlite"
        ApprovalStore(db_path=db, now_fn=clock.now, skip_perm_check=True)
        mode = stat.S_IMODE(db.stat().st_mode)
        assert mode == 0o600, f"expected 0o600, got {oct(mode)}"

    @pytest.mark.skipif(os.name != "posix", reason="POSIX-only")
    def test_rejects_world_readable_existing_file(self, tmp_path, clock):
        db = tmp_path / "approvals.sqlite"
        db.touch()
        os.chmod(db, 0o644)
        with pytest.raises(ApprovalIntegrityError, match="0o644"):
            ApprovalStore(
                db_path=db, now_fn=clock.now, skip_perm_check=False,
            )

    def test_schema_version_recorded(self, store, tmp_path):
        with sqlite3.connect(str(store._db_path)) as conn:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            assert row is not None
            assert row[0] == "1"


# =============================================================================
# create_request — happy paths
# =============================================================================

class TestCreateRequest:

    def test_basic_submit_order_request(self, store):
        rec = store.create_request(
            action_kind=ACTION_SUBMIT_ORDER,
            payload=_basic_payload(),
            requested_by="market_agent",
        )
        assert rec.state == ApprovalState.PROPOSED
        assert rec.action_kind == ACTION_SUBMIT_ORDER
        assert rec.requested_by == "market_agent"
        assert rec.mode == "paper"  # default
        assert rec.payload["symbol"] == "005930"
        assert rec.approval_id.startswith("apv-")
        assert len(rec.approval_id) == len("apv-YYYYMMDD-") + 16

    def test_approval_id_is_unique_under_concurrency(self, store):
        ids: list[str] = []
        lock = threading.Lock()

        def worker():
            for _ in range(20):
                rec = store.create_request(
                    action_kind=ACTION_SUBMIT_ORDER,
                    payload=_basic_payload(),
                    requested_by="agent",
                )
                with lock:
                    ids.append(rec.approval_id)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(ids) == len(set(ids)) == 100

    def test_kill_switch_uses_short_ttl(self, store, clock):
        rec = store.create_request(
            action_kind=ACTION_KILL_SWITCH,
            payload={"activate": True},
            requested_by="risk_agent",
        )
        delta = (rec.expires_at - rec.created_at).total_seconds()
        assert delta == DEFAULT_KILL_SWITCH_TTL_SECONDS

    def test_normal_action_uses_long_ttl(self, store):
        rec = store.create_request(
            action_kind=ACTION_SUBMIT_ORDER,
            payload=_basic_payload(),
            requested_by="agent",
        )
        delta = (rec.expires_at - rec.created_at).total_seconds()
        assert delta == DEFAULT_APPROVAL_TTL_SECONDS

    def test_session_and_trace_ids_stored(self, store):
        rec = store.create_request(
            action_kind=ACTION_SUBMIT_ORDER,
            payload=_basic_payload(),
            requested_by="agent",
            session_id="sess-abc",
            trace_id="trc-xyz",
        )
        assert rec.session_id == "sess-abc"
        assert rec.trace_id == "trc-xyz"

    def test_payload_with_decimal_serialized(self, store):
        # Decimal must round-trip via str()
        payload = {"price_krw": str(Decimal("70000.50")), "qty": 5}
        rec = store.create_request(
            action_kind=ACTION_SUBMIT_ORDER,
            payload=payload,
            requested_by="agent",
        )
        assert rec.payload["price_krw"] == "70000.50"


# =============================================================================
# create_request — validation
# =============================================================================

class TestCreateRequestValidation:

    def test_rejects_invalid_action_kind(self, store):
        with pytest.raises(ApprovalStoreError, match="invalid action_kind"):
            store.create_request(
                action_kind="invalid_action",
                payload=_basic_payload(),
                requested_by="agent",
            )

    def test_rejects_empty_requested_by(self, store):
        with pytest.raises(ApprovalStoreError, match="requested_by"):
            store.create_request(
                action_kind=ACTION_SUBMIT_ORDER,
                payload=_basic_payload(),
                requested_by="",
            )

    def test_rejects_invalid_mode(self, store):
        with pytest.raises(ApprovalStoreError, match="mode must be"):
            store.create_request(
                action_kind=ACTION_SUBMIT_ORDER,
                payload=_basic_payload(),
                requested_by="agent",
                mode="prod",  # legacy name not allowed; must be 'live'
            )

    def test_blocks_live_mode_when_not_allowed(self, store):
        with pytest.raises(LiveModeBlockedError, match="JCPR_ALLOW_LIVE"):
            store.create_request(
                action_kind=ACTION_SUBMIT_ORDER,
                payload=_basic_payload(),
                requested_by="agent",
                mode="live",
            )

    def test_allows_live_mode_when_configured(self, live_store):
        rec = live_store.create_request(
            action_kind=ACTION_SUBMIT_ORDER,
            payload=_basic_payload(),
            requested_by="agent",
            mode="live",
        )
        assert rec.mode == "live"

    def test_rejects_unserializable_payload(self, store):
        with pytest.raises(ApprovalStoreError, match="JSON"):
            store.create_request(
                action_kind=ACTION_SUBMIT_ORDER,
                payload={"obj": object()},  # bare object not serializable
                requested_by="agent",
            )

    def test_rejects_non_mapping_payload(self, store):
        with pytest.raises(ApprovalStoreError, match="payload"):
            store.create_request(
                action_kind=ACTION_SUBMIT_ORDER,
                payload="not_a_dict",  # type: ignore[arg-type]
                requested_by="agent",
            )


# =============================================================================
# Approve / Reject / Cancel
# =============================================================================

class TestApprove:

    def test_approves_proposed(self, store):
        rec = store.create_request(
            action_kind=ACTION_SUBMIT_ORDER,
            payload=_basic_payload(),
            requested_by="market_agent",
        )
        approved = store.approve(
            rec.approval_id, decided_by="alice", reason="LGTM",
        )
        assert approved.state == ApprovalState.APPROVED
        assert approved.decided_by == "alice"
        assert approved.decision_reason == "LGTM"
        assert approved.decided_at is not None
        assert approved.execute_expires_at is not None

    def test_self_approval_blocked(self, store):
        rec = store.create_request(
            action_kind=ACTION_SUBMIT_ORDER,
            payload=_basic_payload(),
            requested_by="alice",  # ← same as decider
        )
        with pytest.raises(SelfApprovalError):
            store.approve(rec.approval_id, decided_by="alice")

    def test_cannot_approve_unknown(self, store):
        with pytest.raises(ApprovalNotFound):
            store.approve("apv-99999999-deadbeefdeadbeef", decided_by="alice")

    def test_cannot_re_approve(self, store):
        rec = store.create_request(
            action_kind=ACTION_SUBMIT_ORDER,
            payload=_basic_payload(),
            requested_by="agent",
        )
        store.approve(rec.approval_id, decided_by="alice")
        with pytest.raises(ApprovalStateError):
            store.approve(rec.approval_id, decided_by="alice")

    def test_cannot_approve_rejected(self, store):
        rec = store.create_request(
            action_kind=ACTION_SUBMIT_ORDER,
            payload=_basic_payload(),
            requested_by="agent",
        )
        store.reject(rec.approval_id, decided_by="alice", reason="no")
        with pytest.raises(ApprovalStateError):
            store.approve(rec.approval_id, decided_by="alice")

    def test_decided_by_required(self, store):
        rec = store.create_request(
            action_kind=ACTION_SUBMIT_ORDER,
            payload=_basic_payload(),
            requested_by="agent",
        )
        with pytest.raises(ApprovalStoreError, match="decided_by"):
            store.approve(rec.approval_id, decided_by="")

    def test_execute_ttl_set_on_approve(self, store, clock):
        rec = store.create_request(
            action_kind=ACTION_SUBMIT_ORDER,
            payload=_basic_payload(),
            requested_by="agent",
        )
        approved = store.approve(rec.approval_id, decided_by="alice")
        delta = (approved.execute_expires_at - approved.decided_at).total_seconds()
        assert delta == DEFAULT_EXECUTE_TTL_SECONDS


class TestReject:

    def test_rejects_proposed(self, store):
        rec = store.create_request(
            action_kind=ACTION_SUBMIT_ORDER,
            payload=_basic_payload(),
            requested_by="agent",
        )
        result = store.reject(
            rec.approval_id, decided_by="alice", reason="too risky",
        )
        assert result.state == ApprovalState.REJECTED
        assert result.decision_reason == "too risky"

    def test_reject_requires_reason(self, store):
        rec = store.create_request(
            action_kind=ACTION_SUBMIT_ORDER,
            payload=_basic_payload(),
            requested_by="agent",
        )
        with pytest.raises(ApprovalStoreError, match="reason"):
            store.reject(rec.approval_id, decided_by="alice", reason="")

    def test_cannot_reject_after_approve(self, store):
        rec = store.create_request(
            action_kind=ACTION_SUBMIT_ORDER,
            payload=_basic_payload(),
            requested_by="agent",
        )
        store.approve(rec.approval_id, decided_by="alice")
        with pytest.raises(ApprovalStateError):
            store.reject(rec.approval_id, decided_by="alice", reason="x")


class TestCancel:

    def test_requester_cancels_proposed(self, store):
        rec = store.create_request(
            action_kind=ACTION_SUBMIT_ORDER,
            payload=_basic_payload(),
            requested_by="agent",
        )
        result = store.cancel(rec.approval_id, cancelled_by="agent")
        assert result.state == ApprovalState.CANCELLED

    def test_cannot_cancel_approved(self, store):
        rec = store.create_request(
            action_kind=ACTION_SUBMIT_ORDER,
            payload=_basic_payload(),
            requested_by="agent",
        )
        store.approve(rec.approval_id, decided_by="alice")
        with pytest.raises(ApprovalStateError):
            store.cancel(rec.approval_id, cancelled_by="agent")


# =============================================================================
# Execution lifecycle
# =============================================================================

class TestExecution:

    def test_approve_then_execute_lifecycle(self, store):
        rec = store.create_request(
            action_kind=ACTION_SUBMIT_ORDER,
            payload=_basic_payload(),
            requested_by="agent",
        )
        store.approve(rec.approval_id, decided_by="alice")
        executing = store.mark_executing(rec.approval_id, executed_by="gateway")
        assert executing.state == ApprovalState.EXECUTING

        executed = store.mark_executed(
            rec.approval_id,
            result={"order_id": "ord-123", "status": "filled"},
        )
        assert executed.state == ApprovalState.EXECUTED
        assert executed.execution_result["order_id"] == "ord-123"

    def test_cannot_execute_proposed(self, store):
        rec = store.create_request(
            action_kind=ACTION_SUBMIT_ORDER,
            payload=_basic_payload(),
            requested_by="agent",
        )
        with pytest.raises(ApprovalStateError):
            store.mark_executing(rec.approval_id, executed_by="gateway")

    def test_cannot_execute_rejected(self, store):
        rec = store.create_request(
            action_kind=ACTION_SUBMIT_ORDER,
            payload=_basic_payload(),
            requested_by="agent",
        )
        store.reject(rec.approval_id, decided_by="alice", reason="no")
        with pytest.raises(ApprovalStateError):
            store.mark_executing(rec.approval_id, executed_by="gateway")

    def test_single_use_execution(self, store):
        rec = store.create_request(
            action_kind=ACTION_SUBMIT_ORDER,
            payload=_basic_payload(),
            requested_by="agent",
        )
        store.approve(rec.approval_id, decided_by="alice")
        store.mark_executing(rec.approval_id, executed_by="gateway")
        store.mark_executed(rec.approval_id, result={"order_id": "ord-1"})

        # Cannot mark_executing again
        with pytest.raises(ApprovalStateError):
            store.mark_executing(rec.approval_id, executed_by="gateway")

    def test_exec_failed_path(self, store):
        rec = store.create_request(
            action_kind=ACTION_SUBMIT_ORDER,
            payload=_basic_payload(),
            requested_by="agent",
        )
        store.approve(rec.approval_id, decided_by="alice")
        store.mark_executing(rec.approval_id, executed_by="gateway")
        failed = store.mark_exec_failed(
            rec.approval_id, error_message="KIS network timeout",
        )
        assert failed.state == ApprovalState.EXEC_FAILED
        assert "timeout" in failed.execution_result["error"]

    def test_cannot_mark_executed_without_executing(self, store):
        rec = store.create_request(
            action_kind=ACTION_SUBMIT_ORDER,
            payload=_basic_payload(),
            requested_by="agent",
        )
        store.approve(rec.approval_id, decided_by="alice")
        # Skip mark_executing
        with pytest.raises(ApprovalStateError):
            store.mark_executed(rec.approval_id, result={"x": 1})

    def test_execute_ttl_enforced(self, store, clock):
        rec = store.create_request(
            action_kind=ACTION_SUBMIT_ORDER,
            payload=_basic_payload(),
            requested_by="agent",
        )
        store.approve(rec.approval_id, decided_by="alice")
        # Advance past execute TTL
        clock.advance(DEFAULT_EXECUTE_TTL_SECONDS + 5)
        with pytest.raises(ApprovalExpiredError):
            store.mark_executing(rec.approval_id, executed_by="gateway")
        # State should now be EXPIRED
        rec_after = store.get(rec.approval_id)
        assert rec_after.state == ApprovalState.EXPIRED


# =============================================================================
# TTL & expiration sweep
# =============================================================================

class TestExpiration:

    def test_expire_overdue_proposed(self, store, clock):
        rec1 = store.create_request(
            action_kind=ACTION_SUBMIT_ORDER,
            payload=_basic_payload(),
            requested_by="agent",
        )
        rec2 = store.create_request(
            action_kind=ACTION_SUBMIT_ORDER,
            payload=_basic_payload(),
            requested_by="agent",
        )
        clock.advance(DEFAULT_APPROVAL_TTL_SECONDS + 10)

        count = store.expire_overdue()
        assert count == 2

        for rid in (rec1.approval_id, rec2.approval_id):
            r = store.get(rid)
            assert r.state == ApprovalState.EXPIRED
            assert "TTL elapsed" in (r.decision_reason or "")

    def test_expire_overdue_approved(self, store, clock):
        rec = store.create_request(
            action_kind=ACTION_SUBMIT_ORDER,
            payload=_basic_payload(),
            requested_by="agent",
        )
        store.approve(rec.approval_id, decided_by="alice")
        clock.advance(DEFAULT_EXECUTE_TTL_SECONDS + 10)
        count = store.expire_overdue()
        assert count == 1
        assert store.get(rec.approval_id).state == ApprovalState.EXPIRED

    def test_expire_overdue_skips_terminal(self, store, clock):
        # An already-rejected request stays REJECTED
        rec = store.create_request(
            action_kind=ACTION_SUBMIT_ORDER,
            payload=_basic_payload(),
            requested_by="agent",
        )
        store.reject(rec.approval_id, decided_by="alice", reason="no")
        clock.advance(99999)
        count = store.expire_overdue()
        assert count == 0
        assert store.get(rec.approval_id).state == ApprovalState.REJECTED

    def test_auto_expire_on_approve_attempt(self, store, clock):
        rec = store.create_request(
            action_kind=ACTION_SUBMIT_ORDER,
            payload=_basic_payload(),
            requested_by="agent",
        )
        clock.advance(DEFAULT_APPROVAL_TTL_SECONDS + 5)
        with pytest.raises(ApprovalExpiredError):
            store.approve(rec.approval_id, decided_by="alice")
        assert store.get(rec.approval_id).state == ApprovalState.EXPIRED


# =============================================================================
# List queries
# =============================================================================

class TestList:

    def test_list_pending(self, store):
        ids = []
        for _ in range(3):
            r = store.create_request(
                action_kind=ACTION_SUBMIT_ORDER,
                payload=_basic_payload(),
                requested_by="agent",
            )
            ids.append(r.approval_id)
        # Reject one
        store.reject(ids[0], decided_by="alice", reason="no")

        pending = store.list_pending()
        pending_ids = {r.approval_id for r in pending}
        assert ids[0] not in pending_ids
        assert ids[1] in pending_ids
        assert ids[2] in pending_ids

    def test_list_pending_filter_by_action(self, store):
        store.create_request(
            action_kind=ACTION_SUBMIT_ORDER,
            payload=_basic_payload(),
            requested_by="agent",
        )
        store.create_request(
            action_kind=ACTION_KILL_SWITCH,
            payload={"activate": True},
            requested_by="risk",
        )
        only_orders = store.list_pending(action_kind=ACTION_SUBMIT_ORDER)
        assert len(only_orders) == 1
        assert only_orders[0].action_kind == ACTION_SUBMIT_ORDER

    def test_list_pending_filter_by_requester(self, store):
        store.create_request(
            action_kind=ACTION_SUBMIT_ORDER,
            payload=_basic_payload(),
            requested_by="market_agent",
        )
        store.create_request(
            action_kind=ACTION_SUBMIT_ORDER,
            payload=_basic_payload(),
            requested_by="risk_agent",
        )
        market_only = store.list_pending(requested_by="market_agent")
        assert len(market_only) == 1
        assert market_only[0].requested_by == "market_agent"

    def test_list_by_state(self, store):
        rec = store.create_request(
            action_kind=ACTION_SUBMIT_ORDER,
            payload=_basic_payload(),
            requested_by="agent",
        )
        store.approve(rec.approval_id, decided_by="alice")
        approved_list = store.list_by_state(ApprovalState.APPROVED)
        assert len(approved_list) == 1
        assert approved_list[0].approval_id == rec.approval_id

    def test_list_limit_validated(self, store):
        with pytest.raises(ApprovalStoreError):
            store.list_pending(limit=0)
        with pytest.raises(ApprovalStoreError):
            store.list_pending(limit=10000)


# =============================================================================
# Concurrency
# =============================================================================

class TestConcurrency:

    def test_only_one_approve_wins(self, tmp_path):
        # Use real wall clock for true concurrency
        store = ApprovalStore(
            db_path=tmp_path / "approvals.sqlite", skip_perm_check=True,
        )
        rec = store.create_request(
            action_kind=ACTION_SUBMIT_ORDER,
            payload=_basic_payload(),
            requested_by="agent",
        )

        results: list[tuple[bool, Exception | None]] = []
        lock = threading.Lock()

        def try_approve(approver: str):
            try:
                store.approve(rec.approval_id, decided_by=approver)
                with lock:
                    results.append((True, None))
            except ApprovalStateError as e:
                with lock:
                    results.append((False, e))
            except SelfApprovalError as e:
                with lock:
                    results.append((False, e))

        threads = [
            threading.Thread(target=try_approve, args=(f"alice{i}",))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        successes = sum(1 for ok, _ in results if ok)
        assert successes == 1, f"expected exactly 1 approve, got {successes}"

    def test_only_one_mark_executing_wins(self, tmp_path):
        store = ApprovalStore(
            db_path=tmp_path / "approvals.sqlite", skip_perm_check=True,
        )
        rec = store.create_request(
            action_kind=ACTION_SUBMIT_ORDER,
            payload=_basic_payload(),
            requested_by="agent",
        )
        store.approve(rec.approval_id, decided_by="alice")

        results: list[bool] = []
        lock = threading.Lock()

        def try_lock():
            try:
                store.mark_executing(rec.approval_id, executed_by="gateway")
                with lock:
                    results.append(True)
            except ApprovalStateError:
                with lock:
                    results.append(False)

        threads = [threading.Thread(target=try_lock) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(results) == 1


# =============================================================================
# Record serialization
# =============================================================================

class TestRecordSerialization:

    def test_to_dict_serializes_datetimes(self, store):
        rec = store.create_request(
            action_kind=ACTION_SUBMIT_ORDER,
            payload=_basic_payload(),
            requested_by="agent",
        )
        d = rec.to_dict()
        assert isinstance(d["created_at"], str)
        assert d["state"] == "proposed"
        # Round-trip via JSON
        reloaded = json.loads(json.dumps(d))
        assert reloaded["state"] == "proposed"

    def test_to_dict_handles_none_datetimes(self, store):
        rec = store.create_request(
            action_kind=ACTION_SUBMIT_ORDER,
            payload=_basic_payload(),
            requested_by="agent",
        )
        d = rec.to_dict()
        assert d["decided_at"] is None
        assert d["executed_at"] is None
