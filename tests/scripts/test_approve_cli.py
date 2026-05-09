"""
tests/scripts/test_approve_cli.py — JCPR-ts-v01 (Phase 2-B)
============================================================

approve_cli 서브커맨드별 동작 검증.

전략:
  - subprocess로 분리 실행하지 않고 main()을 직접 호출 (sys.argv 패치).
  - JCPR_APPROVAL_DB 환경변수로 임시 DB 강제.
  - --broker-factory로 mock broker 주입 (실 KIS 모듈 미필요).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from src.execution.approval_store import ApprovalStatus, ApprovalStore


# ---------------------------------------------------------------------------
# CLI는 import 시점에 sys.path 조정을 하므로, 동일 효과를 위해
# project root 위치 기준으로 import.
# ---------------------------------------------------------------------------
import scripts.approve_cli as cli  # noqa: E402


# Mock broker factory — --broker-factory 인자로 지정될 함수
class _MockBroker:
    def __init__(self, mode):
        self.mode = mode
        self.calls = []
    def submit_order(self, payload):
        self.calls.append(payload)
        return {"broker_order_id": "MOCK-1", "status": "ACCEPTED"}
    def cancel_order(self, payload):
        return {"status": "CANCELLED"}


def mock_factory(cfg) -> tuple:
    """approve_cli --broker-factory에서 호출됨."""
    paper = _MockBroker("paper")
    live = _MockBroker("live") if cfg.mode == "live" else None
    return paper, live


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db = tmp_path / "approvals.sqlite"
    monkeypatch.setenv("JCPR_APPROVAL_DB", str(db))
    return db


@pytest.fixture
def store(tmp_db):
    return ApprovalStore(db_path=tmp_db)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestList:
    def test_list_empty(self, tmp_db, capsys):
        rc = cli.main(["list"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "없음" in out or "no pending" in out

    def test_list_with_proposed(self, tmp_db, store, capsys):
        aid = store.propose(
            action_kind="submit_order",
            payload={"symbol": "005930", "side": "BUY",
                     "qty": 1, "order_type": "MARKET"},
            requested_by="agent",
            mode="paper",
        )
        rc = cli.main(["list"])
        assert rc == 0
        out = capsys.readouterr().out
        assert aid in out


class TestApprove:
    def test_approve_success(self, tmp_db, store, capsys):
        aid = store.propose(
            action_kind="submit_order",
            payload={"symbol": "005930", "side": "BUY",
                     "qty": 1, "order_type": "MARKET"},
            requested_by="agent",
            mode="paper",
        )
        rc = cli.main(["--operator", "alice", "approve", aid])
        assert rc == 0
        rec = store.get(aid)
        assert rec.status == ApprovalStatus.APPROVED
        assert rec.decided_by == "alice"

    def test_approve_self_blocked(self, tmp_db, store):
        aid = store.propose(
            action_kind="submit_order",
            payload={"symbol": "005930", "side": "BUY",
                     "qty": 1, "order_type": "MARKET"},
            requested_by="alice",  # 같은 이름
            mode="paper",
        )
        rc = cli.main(["--operator", "alice", "approve", aid])
        assert rc == 3  # SelfApprovalError 코드

    def test_approve_unknown_id(self, tmp_db):
        rc = cli.main([
            "--operator", "alice", "approve",
            "apv-19700101-deadbeefcafebabe",
        ])
        assert rc == 2


class TestReject:
    def test_reject_with_reason(self, tmp_db, store):
        aid = store.propose(
            action_kind="submit_order",
            payload={"symbol": "005930", "side": "BUY",
                     "qty": 1, "order_type": "MARKET"},
            requested_by="agent",
            mode="paper",
        )
        rc = cli.main([
            "--operator", "alice", "reject", aid,
            "--reason", "too risky",
        ])
        assert rc == 0
        rec = store.get(aid)
        assert rec.status == ApprovalStatus.REJECTED
        assert rec.reason == "too risky"


class TestCancel:
    def test_cancel_proposed(self, tmp_db, store):
        aid = store.propose(
            action_kind="submit_order",
            payload={"symbol": "005930", "side": "BUY",
                     "qty": 1, "order_type": "MARKET"},
            requested_by="agent",
            mode="paper",
        )
        rc = cli.main(["--operator", "alice", "cancel", aid])
        assert rc == 0
        rec = store.get(aid)
        assert rec.status == ApprovalStatus.CANCELLED


class TestExecute:
    def test_execute_with_yes_flag(self, tmp_db, store, capsys):
        aid = store.propose(
            action_kind="submit_order",
            payload={"symbol": "005930", "side": "BUY",
                     "qty": 1, "order_type": "MARKET"},
            requested_by="agent",
            mode="paper",
        )
        store.approve(aid, decided_by="alice")
        rc = cli.main([
            "--operator", "alice", "execute", aid,
            "--yes",
            "--broker-factory", "tests.scripts.test_approve_cli:mock_factory",
        ])
        assert rc == 0
        rec = store.get(aid)
        assert rec.status == ApprovalStatus.EXECUTED

    def test_execute_not_approved(self, tmp_db, store):
        aid = store.propose(
            action_kind="submit_order",
            payload={"symbol": "005930", "side": "BUY",
                     "qty": 1, "order_type": "MARKET"},
            requested_by="agent",
            mode="paper",
        )
        # PROPOSED 상태에서 execute 시도
        rc = cli.main([
            "--operator", "alice", "execute", aid,
            "--yes",
            "--broker-factory", "tests.scripts.test_approve_cli:mock_factory",
        ])
        assert rc == 6  # APPROVED 아님 코드


class TestRequiredArgs:
    def test_mutating_requires_operator(self, tmp_db, store, capsys):
        aid = store.propose(
            action_kind="submit_order",
            payload={"symbol": "005930", "side": "BUY",
                     "qty": 1, "order_type": "MARKET"},
            requested_by="agent",
            mode="paper",
        )
        with pytest.raises(SystemExit):
            cli.main(["approve", aid])  # --operator 누락

    def test_list_no_operator_ok(self, tmp_db):
        rc = cli.main(["list"])
        assert rc == 0


class TestShow:
    def test_show_existing(self, tmp_db, store, capsys):
        aid = store.propose(
            action_kind="submit_order",
            payload={"symbol": "005930", "side": "BUY",
                     "qty": 1, "order_type": "MARKET"},
            requested_by="agent",
            mode="paper",
        )
        rc = cli.main(["show", aid])
        assert rc == 0
        out = capsys.readouterr().out
        assert aid in out
        assert "005930" in out

    def test_show_unknown(self, tmp_db):
        rc = cli.main(["show", "apv-19700101-deadbeefcafebabe"])
        assert rc == 2
