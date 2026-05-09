
"""
스모크 테스트 — _approval_store
=================================

JCPR Trading System - jcpr-ts-v01
Task 35 v0.1
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

    ACTION_CANCEL_ORDER,
    ACTION_KILL_SWITCH,
    ACTION_SET_CAPACITY,
    ACTION_SUBMIT_ORDER,
    STATUS_APPROVED,
    STATUS_CANCELLED,
    STATUS_EXECUTED,
    STATUS_EXPIRED,
    STATUS_PENDING,
    STATUS_REJECTED,
    ApprovalNotFound,
    ApprovalStateError,
    ApprovalStore,
    ApprovalStoreError,
    SelfApprovalError,
    generate_approval_id,
)


# ─────────────────────────────────────────────────
# ID
# ─────────────────────────────────────────────────

def test_generate_approval_id_format():
    aid = generate_approval_id()
    assert aid.startswith("apv-")
    parts = aid.split("-")
    assert len(parts) == 3
    assert len(parts[1]) == 8  # YYYYMMDD
    assert len(parts[2]) == 8  # hex
    int(parts[2], 16)
    print("✅ test_generate_approval_id_format")


def test_approval_id_uniqueness():
    ids = {generate_approval_id() for _ in range(100)}
    assert len(ids) == 100
    print("✅ test_approval_id_uniqueness")


# ─────────────────────────────────────────────────
# 기본 라이프사이클
# ─────────────────────────────────────────────────

def test_create_request(tmp_dir):
    store = ApprovalStore(db_path=str(tmp_dir / "ap.db"))
    rec = store.create_request(
        action_type=ACTION_SUBMIT_ORDER,
        requested_by="agent_alice",
        payload={"symbol": "005930", "qty": 10},
        trace_id="trc-20260507-deadbeef",
    )
    assert rec.status == STATUS_PENDING
    assert rec.action_type == ACTION_SUBMIT_ORDER
    assert rec.requested_by == "agent_alice"
    assert rec.payload["symbol"] == "005930"
    assert rec.paper_mode is True  # default
    assert rec.expires_at_utc > rec.requested_at_utc
    print("✅ test_create_request")


def test_approve_flow(tmp_dir):
    store = ApprovalStore(db_path=str(tmp_dir / "ap.db"))
    rec = store.create_request(
        action_type=ACTION_SUBMIT_ORDER,
        requested_by="agent",
        payload={},
        trace_id="trc-20260507-aaaaaaaa",
    )
    approved = store.approve(
        rec.approval_id,
        decided_by="operator_bob",
        reason="verified",
    )
    assert approved.status == STATUS_APPROVED
    assert approved.decided_by == "operator_bob"
    assert approved.decision_reason == "verified"
    assert approved.decided_at_utc is not None
    print("✅ test_approve_flow")


def test_reject_flow(tmp_dir):
    store = ApprovalStore(db_path=str(tmp_dir / "ap.db"))
    rec = store.create_request(
        action_type=ACTION_KILL_SWITCH,
        requested_by="agent",
        payload={"activate": True, "reason": "test"},
        trace_id="trc-20260507-bbbbbbbb",
    )
    rejected = store.reject(
        rec.approval_id,
        decided_by="operator",
        reason="not_warranted",
    )
    assert rejected.status == STATUS_REJECTED
    print("✅ test_reject_flow")


def test_execute_flow(tmp_dir):
    store = ApprovalStore(db_path=str(tmp_dir / "ap.db"))
    rec = store.create_request(
        action_type=ACTION_SUBMIT_ORDER,
        requested_by="agent",
        payload={"symbol": "005930"},
        trace_id="trc-20260507-cccccccc",
    )
    store.approve(rec.approval_id, decided_by="operator")
    executed = store.mark_executed(
        rec.approval_id,
        execution_result={"executed": True, "broker_order_id": "B123"},
        executed_by="agent",
    )
    assert executed.status == STATUS_EXECUTED
    assert executed.execution_result["broker_order_id"] == "B123"
    assert executed.executed_at_utc is not None
    print("✅ test_execute_flow")


def test_cancel_by_requester(tmp_dir):
    store = ApprovalStore(db_path=str(tmp_dir / "ap.db"))
    rec = store.create_request(
        action_type=ACTION_SUBMIT_ORDER,
        requested_by="agent_x",
        payload={},
        trace_id="trc-20260507-dddddddd",
    )
    cancelled = store.cancel(
        rec.approval_id,
        cancelled_by="agent_x",
        reason="changed mind",
    )
    assert cancelled.status == STATUS_CANCELLED
    print("✅ test_cancel_by_requester")


def test_cancel_by_other_rejected(tmp_dir):
    """다른 사람이 cancel 시 거부."""
    store = ApprovalStore(db_path=str(tmp_dir / "ap.db"))
    rec = store.create_request(
        action_type=ACTION_SUBMIT_ORDER,
        requested_by="agent_x",
        payload={},
        trace_id="trc-20260507-11111111",
    )
    try:
        store.cancel(
            rec.approval_id,
            cancelled_by="agent_y",  # 다른 사람
            reason="test",
        )
        assert False
    except ApprovalStoreError as e:
        assert "requester" in str(e).lower()
    print("✅ test_cancel_by_other_rejected")


# ─────────────────────────────────────────────────
# 보안: Self-Approval Blocked
# ─────────────────────────────────────────────────

def test_self_approval_blocked(tmp_dir):
    """기본은 self-approval 차단."""
    store = ApprovalStore(db_path=str(tmp_dir / "ap.db"))
    rec = store.create_request(
        action_type=ACTION_SUBMIT_ORDER,
        requested_by="alice",
        payload={},
        trace_id="trc-20260507-22222222",
    )
    try:
        store.approve(rec.approval_id, decided_by="alice")  # 자기 자신
        assert False, "Self-approval should be blocked"
    except SelfApprovalError:
        pass
    print("✅ test_self_approval_blocked")


def test_self_approval_allowed_when_configured(tmp_dir):
    """allow_self_approval=True 시 허용."""
    store = ApprovalStore(
        db_path=str(tmp_dir / "ap.db"),
        allow_self_approval=True,
    )
    rec = store.create_request(
        action_type=ACTION_SUBMIT_ORDER,
        requested_by="alice",
        payload={},
        trace_id="trc-20260507-33333333",
    )
    approved = store.approve(rec.approval_id, decided_by="alice")
    assert approved.status == STATUS_APPROVED
    print("✅ test_self_approval_allowed_when_configured")


# ─────────────────────────────────────────────────
# 상태 전이 검증
# ─────────────────────────────────────────────────

def test_double_approve_rejected(tmp_dir):
    store = ApprovalStore(db_path=str(tmp_dir / "ap.db"))
    rec = store.create_request(
        action_type=ACTION_SUBMIT_ORDER,
        requested_by="agent",
        payload={},
        trace_id="trc-20260507-44444444",
    )
    store.approve(rec.approval_id, decided_by="op")
    try:
        store.approve(rec.approval_id, decided_by="op2")
        assert False
    except ApprovalStateError:
        pass
    print("✅ test_double_approve_rejected")


def test_execute_without_approval_rejected(tmp_dir):
    store = ApprovalStore(db_path=str(tmp_dir / "ap.db"))
    rec = store.create_request(
        action_type=ACTION_SUBMIT_ORDER,
        requested_by="agent",
        payload={},
        trace_id="trc-20260507-55555555",
    )
    try:
        store.mark_executed(
            rec.approval_id,
            execution_result={},
            executed_by="agent",
        )
        assert False
    except ApprovalStateError:
        pass
    print("✅ test_execute_without_approval_rejected")


def test_double_execute_rejected(tmp_dir):
    """이미 executed인 것은 재실행 불가."""
    store = ApprovalStore(db_path=str(tmp_dir / "ap.db"))
    rec = store.create_request(
        action_type=ACTION_SUBMIT_ORDER,
        requested_by="agent",
        payload={},
        trace_id="trc-20260507-66666666",
    )
    store.approve(rec.approval_id, decided_by="op")
    store.mark_executed(rec.approval_id, execution_result={"ok": True}, executed_by="agent")
    try:
        store.mark_executed(rec.approval_id, execution_result={}, executed_by="agent")
        assert False
    except ApprovalStateError:
        pass
    print("✅ test_double_execute_rejected")


# ─────────────────────────────────────────────────
# 만료
# ─────────────────────────────────────────────────

def test_auto_expiry_on_get(tmp_dir):
    """TTL 지난 후 get 시 expired로."""
    store = ApprovalStore(
        db_path=str(tmp_dir / "ap.db"),
        default_ttl_seconds=5,
    )
    # 과거 시각으로 만든 요청 (직접 시뮬레이션 어려우므로 짧은 TTL + sleep)
    # 여기서는 5초 sleep 대신 manual now_utc 주입 — store API에 없음
    # 대신: 매우 짧은 TTL + 시간 진행 시뮬레이션
    rec = store.create_request(
        action_type=ACTION_SUBMIT_ORDER,
        requested_by="agent",
        payload={},
        trace_id="trc-20260507-77777777",
        custom_ttl_seconds=5,
    )
    # 직접 expires_at_utc 를 과거로 update
    import sqlite3
    conn = sqlite3.connect(str(tmp_dir / "ap.db"))
    conn.execute(
        "UPDATE approvals SET expires_at_utc = ? WHERE approval_id = ?",
        ("2020-01-01T00:00:00+00:00", rec.approval_id),
    )
    conn.commit()
    conn.close()

    # get 시 자동 만료 처리
    fetched = store.get(rec.approval_id)
    assert fetched.status == STATUS_EXPIRED
    print("✅ test_auto_expiry_on_get")


def test_execute_after_execute_ttl_expired(tmp_dir):
    """승인 후 execute_ttl 지나면 실행 불가."""
    store = ApprovalStore(
        db_path=str(tmp_dir / "ap.db"),
        default_ttl_seconds=300,
        execute_ttl_seconds=10,
    )
    rec = store.create_request(
        action_type=ACTION_SUBMIT_ORDER,
        requested_by="agent",
        payload={},
        trace_id="trc-20260507-88888888",
    )
    store.approve(rec.approval_id, decided_by="op")

    # decided_at을 과거로 변경 (15초 전)
    import sqlite3
    past = (datetime.now(timezone.utc) - timedelta(seconds=15)).isoformat()
    conn = sqlite3.connect(str(tmp_dir / "ap.db"))
    conn.execute(
        "UPDATE approvals SET decided_at_utc = ? WHERE approval_id = ?",
        (past, rec.approval_id),
    )
    conn.commit()
    conn.close()

    try:
        store.mark_executed(
            rec.approval_id,
            execution_result={},
            executed_by="agent",
        )
        assert False, "Should be expired"
    except ApprovalStateError as e:
        assert "expired" in str(e).lower() or "ttl" in str(e).lower()
    # 자동 expired 상태로
    rec2 = store.get(rec.approval_id)
    assert rec2.status == STATUS_EXPIRED
    print("✅ test_execute_after_execute_ttl_expired")


# ─────────────────────────────────────────────────
# 조회
# ─────────────────────────────────────────────────

def test_list_pending(tmp_dir):
    store = ApprovalStore(db_path=str(tmp_dir / "ap.db"))
    for i in range(3):
        store.create_request(
            action_type=ACTION_SUBMIT_ORDER,
            requested_by=f"agent_{i}",
            payload={"i": i},
            trace_id=f"trc-20260507-{i:08x}",
        )
    pending = store.list_pending()
    assert len(pending) == 3
    print("✅ test_list_pending")


def test_list_excludes_terminal(tmp_dir):
    store = ApprovalStore(db_path=str(tmp_dir / "ap.db"))
    rec1 = store.create_request(
        action_type=ACTION_SUBMIT_ORDER,
        requested_by="agent",
        payload={},
        trace_id="trc-20260507-aaaa1111",
    )
    rec2 = store.create_request(
        action_type=ACTION_SUBMIT_ORDER,
        requested_by="agent",
        payload={},
        trace_id="trc-20260507-aaaa2222",
    )
    store.reject(rec1.approval_id, decided_by="op")
    pending = store.list_pending()
    assert len(pending) == 1
    assert pending[0].approval_id == rec2.approval_id
    print("✅ test_list_excludes_terminal")


def test_list_by_requester(tmp_dir):
    store = ApprovalStore(db_path=str(tmp_dir / "ap.db"))
    store.create_request(
        action_type=ACTION_SUBMIT_ORDER,
        requested_by="alice",
        payload={},
        trace_id="trc-20260507-aaaa3333",
    )
    store.create_request(
        action_type=ACTION_SUBMIT_ORDER,
        requested_by="bob",
        payload={},
        trace_id="trc-20260507-aaaa4444",
    )
    alice_recs = store.list_by_requester("alice")
    assert len(alice_recs) == 1
    assert alice_recs[0].requested_by == "alice"
    print("✅ test_list_by_requester")


# ─────────────────────────────────────────────────
# 검증
# ─────────────────────────────────────────────────

def test_invalid_action_type(tmp_dir):
    store = ApprovalStore(db_path=str(tmp_dir / "ap.db"))
    try:
        store.create_request(
            action_type="invalid_type",
            requested_by="agent",
            payload={},
            trace_id="trc-20260507-11112222",
        )
        assert False
    except ApprovalStoreError:
        pass
    print("✅ test_invalid_action_type")


def test_get_nonexistent(tmp_dir):
    store = ApprovalStore(db_path=str(tmp_dir / "ap.db"))
    try:
        store.get("apv-20991231-deadbeef")
        assert False
    except ApprovalNotFound:
        pass
    # get_optional은 None
    assert store.get_optional("apv-20991231-deadbeef") is None
    print("✅ test_get_nonexistent")


def test_record_to_dict_serializable(tmp_dir):
    import json
    store = ApprovalStore(db_path=str(tmp_dir / "ap.db"))
    rec = store.create_request(
        action_type=ACTION_SUBMIT_ORDER,
        requested_by="agent",
        payload={"symbol": "005930", "price_krw": "70000"},
        trace_id="trc-20260507-aaaa5555",
    )
    d = rec.to_dict()
    j = json.dumps(d, ensure_ascii=False)
    assert "005930" in j
    assert "approval_id" in d
    print("✅ test_record_to_dict_serializable")


# ─────────────────────────────────────────────────
# 실행
# ─────────────────────────────────────────────────

try:
    import pytest

    @pytest.fixture
    def tmp_dir(tmp_path):
        return tmp_path
except ImportError:
    pass


def _run_all() -> int:
    failed = 0
    no_arg = [
        test_generate_approval_id_format,
        test_approval_id_uniqueness,
    ]
    arg_tests = [
        test_create_request, test_approve_flow, test_reject_flow,
        test_execute_flow, test_cancel_by_requester,
        test_cancel_by_other_rejected,
        test_self_approval_blocked, test_self_approval_allowed_when_configured,
        test_double_approve_rejected, test_execute_without_approval_rejected,
        test_double_execute_rejected,
        test_auto_expiry_on_get, test_execute_after_execute_ttl_expired,
        test_list_pending, test_list_excludes_terminal, test_list_by_requester,
        test_invalid_action_type, test_get_nonexistent,
        test_record_to_dict_serializable,
    ]
    for fn in no_arg:
        try:
            fn()
        except AssertionError as e:
            print(f"❌ {fn.__name__}: {e}")
            failed += 1
        except Exception as e:  # noqa: BLE001
            print(f"💥 {fn.__name__}: {type(e).__name__}: {e}")
            failed += 1
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        for fn in arg_tests:
            sub = td_path / fn.__name__
            sub.mkdir()
            try:
                fn(sub)
            except AssertionError as e:
                print(f"❌ {fn.__name__}: {e}")
                failed += 1
            except Exception as e:  # noqa: BLE001
                print(f"💥 {fn.__name__}: {type(e).__name__}: {e}")
                failed += 1
    return failed


if __name__ == "__main__":
    print("─" * 50)
    print("Task 35 v0.1 — _approval_store 스모크 테스트")
    print("─" * 50)
    failed = _run_all()
    print("─" * 50)
    if failed == 0:
        print("✅ 모든 테스트 통과")
        sys.exit(0)
    else:
        print(f"❌ {failed}개 실패")
        sys.exit(1)
