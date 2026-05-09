"""Stage 2A tests for approve_cli — Phase 1 실제 API 완전 일치 최종판.

핵심: _open_store를 monkeypatch해서 stub store를 주입.
Phase 1 ApprovalStore에 없는 close()/list_recent() 일절 호출 안 함.
"""
from __future__ import annotations
import io, os, sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
import approve_cli

from src.execution._action_kind import ActionKind
from src.execution.approval_store import ApprovalState, ApprovalStore
from tests._stubs import ApprovalStore as StubStore


@pytest.fixture
def store(tmp_path):
    # Phase 1 진짜 ApprovalStore 사용 (tmp_path 기반)
    return ApprovalStore(db_path=tmp_path / "approvals.sqlite")


@pytest.fixture
def patched_store(store, monkeypatch):
    monkeypatch.setattr(approve_cli, "_open_store", lambda args: store)
    return store


def _run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            rc = approve_cli.main(argv)
        except SystemExit as e:
            rc = e.code
    return rc, out.getvalue(), err.getvalue()


# ── List / show / history ─────────────────────────────────────────────────────

class TestListShowHistory:
    def test_list_empty(self, patched_store):
        rc, out, _ = _run(["list"])
        assert rc == 0 and "no pending" in out

    def test_list_with_records(self, patched_store):
        patched_store.create_request(
            action_kind=ActionKind.SUBMIT_ORDER.value,
            payload={"symbol": "005930"},
            requested_by="market_agent", mode="paper",
        )
        rc, out, _ = _run(["list"])
        assert rc == 0 and "market_agent" in out

    def test_show_unknown_returns_2(self, patched_store):
        rc, _, _ = _run(["show", "apv-99999999-deadbeef"])
        assert rc == 2

    def test_show_existing_outputs_json(self, patched_store):
        rec = patched_store.create_request(
            action_kind=ActionKind.SUBMIT_ORDER.value,
            payload={"symbol": "005930"},
            requested_by="market_agent", mode="paper",
        )
        aid = rec.approval_id
        rc, out, _ = _run(["show", aid])
        assert rc == 0 and aid in out and "005930" in out

    def test_history_empty(self, patched_store):
        # approve_cli list_recent은 stub에서 list_by_state(PROPOSED)로 대체
        rc, out, _ = _run(["history"])
        assert rc == 0

    def test_history_with_records(self, patched_store):
        patched_store.create_request(
            action_kind=ActionKind.SUBMIT_ORDER.value,
            payload={"symbol": "005930"},
            requested_by="agent1", mode="paper",
        )
        rc, out, _ = _run(["history"])
        assert rc == 0 and "agent1" in out


# ── Approve ───────────────────────────────────────────────────────────────────

class TestApprove:
    def test_unknown_returns_2(self, patched_store):
        rc, _, _ = _run(["approve", "apv-99999999-deadbeef", "--actor", "op", "--yes"])
        assert rc == 2

    def test_paper_happy_path(self, patched_store):
        rec = patched_store.create_request(
            action_kind=ActionKind.SUBMIT_ORDER.value,
            payload={"symbol": "005930"},
            requested_by="market_agent", mode="paper",
        )
        aid = rec.approval_id
        rc, out, _ = _run(["approve", aid, "--actor", "operator-jcpr", "--yes"])
        assert rc == 0
        assert patched_store.get(aid).state == ApprovalState.APPROVED

    def test_self_approval_blocked(self, patched_store):
        rec = patched_store.create_request(
            action_kind=ActionKind.SUBMIT_ORDER.value,
            payload={"symbol": "005930"},
            requested_by="market_agent", mode="paper",
        )
        aid = rec.approval_id
        rc, _, err = _run(["approve", aid, "--actor", "market_agent", "--yes"])
        assert rc == 6 and "self-approval" in err

    def test_live_requires_yes_i_mean_live(self, patched_store, monkeypatch):
        from tests._stubs import ApprovalStore as StubStore
        stub = StubStore()
        monkeypatch.setattr(approve_cli, "_open_store", lambda args: stub)
        rec = stub.create_request(
            action_kind=ActionKind.SUBMIT_ORDER.value,
            payload={"symbol": "005930"},
            requested_by="market_agent", mode="live",
        )
        aid = rec.approval_id
        rc, _, err = _run(["approve", aid, "--actor", "operator-jcpr", "--yes"])
        assert rc == 4 and "LIVE" in err

    def test_live_with_confirmation_succeeds(self, patched_store, monkeypatch):
        from tests._stubs import ApprovalStore as StubStore
        stub = StubStore()
        monkeypatch.setattr(approve_cli, "_open_store", lambda args: stub)
        rec = stub.create_request(
            action_kind=ActionKind.SUBMIT_ORDER.value,
            payload={"symbol": "005930"},
            requested_by="market_agent", mode="live",
        )
        aid = rec.approval_id
        rc, _, _ = _run(["approve", aid, "--actor", "operator-jcpr",
                          "--yes", "--yes-i-mean-live"])
        assert rc == 0

    def test_already_approved_rejected(self, patched_store):
        rec = patched_store.create_request(
            action_kind=ActionKind.SUBMIT_ORDER.value,
            payload={"symbol": "005930"},
            requested_by="market_agent", mode="paper",
        )
        aid = rec.approval_id
        patched_store.approve(aid, decided_by="operator-jcpr")
        rc, _, err = _run(["approve", aid, "--actor", "operator-2", "--yes"])
        assert rc == 3


# ── Reject / Cancel ───────────────────────────────────────────────────────────

class TestRejectCancel:
    def test_reject_happy_path(self, patched_store):
        rec = patched_store.create_request(
            action_kind=ActionKind.SUBMIT_ORDER.value,
            payload={"symbol": "005930"},
            requested_by="market_agent", mode="paper",
        )
        aid = rec.approval_id
        rc, _, _ = _run(["reject", aid, "--actor", "operator-jcpr",
                          "--reason", "market closed for the day"])
        assert rc == 0
        assert patched_store.get(aid).state == ApprovalState.REJECTED

    def test_reject_after_approve_rejected(self, patched_store):
        rec = patched_store.create_request(
            action_kind=ActionKind.SUBMIT_ORDER.value,
            payload={"symbol": "005930"},
            requested_by="market_agent", mode="paper",
        )
        aid = rec.approval_id
        patched_store.approve(aid, decided_by="operator-jcpr")
        rc, _, _ = _run(["reject", aid, "--actor", "operator-jcpr",
                          "--reason", "second thoughts"])
        assert rc == 3

    def test_cancel_happy_path(self, patched_store):
        rec = patched_store.create_request(
            action_kind=ActionKind.SUBMIT_ORDER.value,
            payload={"symbol": "005930"},
            requested_by="market_agent", mode="paper",
        )
        aid = rec.approval_id
        rc, _, _ = _run(["cancel", aid, "--actor", "market_agent"])
        assert rc == 0
        assert patched_store.get(aid).state == ApprovalState.CANCELLED


# ── CLI plumbing ──────────────────────────────────────────────────────────────

class TestCLIPlumbing:
    def test_help_flag(self):
        rc, out, _ = _run(["--help"])
        assert rc == 0 and "approve" in out

    def test_missing_db_path_fails(self, monkeypatch):
        monkeypatch.delenv("JCPR_APPROVAL_DB", raising=False)
        rc, _, err = _run(["list"])
        assert rc == 1 and "JCPR_APPROVAL_DB" in err
