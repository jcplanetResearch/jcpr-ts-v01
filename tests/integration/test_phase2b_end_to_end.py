"""
tests/integration/test_phase2b_end_to_end.py — JCPR-ts-v01 (Phase 2-B)
=======================================================================

End-to-end 통합 테스트 — agent가 propose하고, 운영자가 approve_cli로
승인하고, ExecutionGateway가 KIS mock으로 집행하는 전체 워크플로우를
단일 SQLite DB 위에서 검증.

검증 시나리오 (scenarios):
  1. happy path — propose → approve → execute → EXECUTED
  2. reject 흐름 — propose → reject → 재propose는 새 ID
  3. cancel 흐름 — agent가 자기 propose를 cancel
  4. self-approval 차단 — operator=requested_by 시 거부
  5. live 모드 가드 — paper config에서 live propose는 ModeViolationError
  6. kill switch 활성 시 모든 execute 차단
  7. PROPOSED TTL 만료 — 5분 후 EXPIRED, approve 거부
  8. APPROVED TTL 만료 — 60초 후 EXPIRED, execute 거부
  9. EXEC_FAILED 후 재실행 차단
 10. 동시 운영자 2명이 같은 ID approve 시도 — 한 명만 성공
 11. 시크릿 누설 차단 종합
 12. paper/live 모드 일치성 — record.mode와 gateway.mode 매칭
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from src.execution.approval_store import (
    ApprovalNotFoundError,
    ApprovalStatus,
    ApprovalStore,
    InvalidTransitionError,
    SelfApprovalError,
)
from src.execution.execution_gateway import (
    AlreadyExecutedError,
    AlreadyExecutingError,
    BrokerExecutionError,
    ExecutionGateway,
    ExpiredApprovalError,
    KillSwitchActiveError,
    ModeViolationError,
)
from src.mcp_servers._config import ServerConfig
from src.mcp_servers._write_handlers import WriteHandlers
from src.mcp_servers.restricted_server import (
    RestrictedServer,
    build_restricted_server,
)


# ---------------------------------------------------------------------------
# Mock KIS broker — 실 API 미사용
# ---------------------------------------------------------------------------

class MockKISBroker:
    """KIS API 응답을 모사하는 가짜 어댑터."""

    def __init__(self, mode: str = "paper", *, fail_on_symbol: str | None = None) -> None:
        self.mode = mode
        self.fail_on_symbol = fail_on_symbol
        self.submit_log: list[dict] = []
        self.cancel_log: list[dict] = []
        self._counter = 0

    def submit_order(self, payload: dict) -> dict:
        self.submit_log.append(dict(payload))
        if self.fail_on_symbol and payload.get("symbol") == self.fail_on_symbol:
            raise RuntimeError(f"KIS API rejected symbol={payload['symbol']}")
        self._counter += 1
        return {
            "broker_order_id": f"KIS-{self.mode.upper()}-{self._counter:08d}",
            "status": "ACCEPTED",
            "echo": payload,
        }

    def cancel_order(self, payload: dict) -> dict:
        self.cancel_log.append(dict(payload))
        return {"status": "CANCELLED", "broker_order_id": payload["broker_order_id"]}


class ToggleKillSwitch:
    def __init__(self, active: bool = False) -> None:
        self._active = active
    def set(self, v: bool) -> None:
        self._active = v
    def is_active(self) -> bool:
        return self._active


# ---------------------------------------------------------------------------
# Fixtures — 전체 시스템 와이어업
# ---------------------------------------------------------------------------

@pytest.fixture
def paper_system(tmp_path):
    """paper 모드 전체 시스템 (server + gateway + same store)."""
    cfg = ServerConfig(
        approval_db_path=tmp_path / "approvals.sqlite",
        mode="paper", allow_live=False, project_root=tmp_path,
    )
    store = ApprovalStore(db_path=cfg.approval_db_path)
    paper_broker = MockKISBroker(mode="paper")
    kill = ToggleKillSwitch()
    gateway = ExecutionGateway(
        store=store, paper_broker=paper_broker,
        kill_switch=kill, mode="paper",
    )
    handlers = WriteHandlers(store=store, mode="paper")
    server = RestrictedServer(
        config=cfg, store=store, gateway=gateway, handlers=handlers,
    )
    return {
        "cfg": cfg, "store": store, "broker": paper_broker,
        "kill": kill, "gateway": gateway, "server": server,
    }


@pytest.fixture
def live_system(tmp_path):
    cfg = ServerConfig(
        approval_db_path=tmp_path / "approvals.sqlite",
        mode="live", allow_live=True, project_root=tmp_path,
    )
    store = ApprovalStore(db_path=cfg.approval_db_path)
    paper_broker = MockKISBroker(mode="paper")
    live_broker = MockKISBroker(mode="live")
    gateway = ExecutionGateway(
        store=store, paper_broker=paper_broker, live_broker=live_broker,
        mode="live", allow_live=True,
    )
    handlers = WriteHandlers(store=store, mode="live")
    server = RestrictedServer(
        config=cfg, store=store, gateway=gateway, handlers=handlers,
    )
    return {
        "cfg": cfg, "store": store, "paper_broker": paper_broker,
        "live_broker": live_broker, "gateway": gateway, "server": server,
    }


# ===========================================================================
# 1. Happy path
# ===========================================================================

class TestHappyPath:
    def test_full_flow_propose_approve_execute(self, paper_system):
        """agent → operator approve → operator execute → EXECUTED."""
        sys = paper_system

        # [agent] propose
        r = sys["server"].call_tool(
            "propose_submit_order",
            payload={"symbol": "005930", "side": "BUY",
                     "qty": 10, "order_type": "LIMIT",
                     "limit_price": 70000},
            requested_by="market_agent",
        )
        assert r.ok
        aid = r.result["approval_id"]

        # [operator] approve (approve_cli 동등 — store 직접 호출)
        sys["store"].approve(aid, decided_by="alice")
        rec = sys["store"].get(aid)
        assert rec.status == ApprovalStatus.APPROVED

        # [operator] execute
        result = sys["gateway"].execute(aid)
        assert result.final_status == "EXECUTED"
        assert result.broker_response["status"] == "ACCEPTED"

        # 브로커 호출 1회
        assert len(sys["broker"].submit_log) == 1
        assert sys["broker"].submit_log[0]["symbol"] == "005930"

        # [agent] query
        r2 = sys["server"].call_tool("query_approval_status", approval_id=aid)
        assert r2.result["status"] == "EXECUTED"
        assert r2.result["broker_response"]["status"] == "ACCEPTED"


# ===========================================================================
# 2. Reject 흐름
# ===========================================================================

class TestRejectFlow:
    def test_reject_then_repropose_is_new_id(self, paper_system):
        sys = paper_system

        r1 = sys["server"].call_tool(
            "propose_submit_order",
            payload={"symbol": "005930", "side": "BUY",
                     "qty": 10, "order_type": "MARKET"},
            requested_by="agent",
        )
        aid1 = r1.result["approval_id"]
        sys["store"].reject(aid1, decided_by="alice", reason="too risky")

        # 재propose
        r2 = sys["server"].call_tool(
            "propose_submit_order",
            payload={"symbol": "005930", "side": "BUY",
                     "qty": 10, "order_type": "MARKET"},
            requested_by="agent",
        )
        aid2 = r2.result["approval_id"]
        assert aid2 != aid1

        # 브로커 미호출
        assert len(sys["broker"].submit_log) == 0


# ===========================================================================
# 3. Cancel
# ===========================================================================

class TestCancelFlow:
    def test_agent_cancels_own_proposal(self, paper_system):
        sys = paper_system
        r = sys["server"].call_tool(
            "propose_submit_order",
            payload={"symbol": "005930", "side": "BUY",
                     "qty": 1, "order_type": "MARKET"},
            requested_by="agent_a",
        )
        aid = r.result["approval_id"]
        cr = sys["server"].call_tool(
            "cancel_proposal", approval_id=aid, requested_by="agent_a",
        )
        assert cr.ok
        rec = sys["store"].get(aid)
        assert rec.status == ApprovalStatus.CANCELLED

    def test_other_agent_cannot_cancel(self, paper_system):
        sys = paper_system
        r = sys["server"].call_tool(
            "propose_submit_order",
            payload={"symbol": "005930", "side": "BUY",
                     "qty": 1, "order_type": "MARKET"},
            requested_by="agent_a",
        )
        aid = r.result["approval_id"]
        cr = sys["server"].call_tool(
            "cancel_proposal", approval_id=aid, requested_by="agent_b",
        )
        assert not cr.ok
        assert cr.error_kind == "identity"


# ===========================================================================
# 4. Self-approval 차단
# ===========================================================================

class TestSelfApprovalBlock:
    def test_operator_cannot_self_approve(self, paper_system):
        sys = paper_system
        # 만약 어떤 식으로든 operator가 propose했다면(operator로 시작하는
        # 이름은 server에서 차단되지만, 원리적으로 store는 보호해야 함)
        # store에 직접 propose
        aid = sys["store"].propose(
            action_kind="submit_order",
            payload={"symbol": "005930", "side": "BUY",
                     "qty": 1, "order_type": "MARKET"},
            requested_by="alice",  # operator의 본명
            mode="paper",
        )
        with pytest.raises(SelfApprovalError):
            sys["store"].approve(aid, decided_by="alice")


# ===========================================================================
# 5. Live mode guard
# ===========================================================================

class TestLiveModeGuard:
    def test_paper_handler_creates_paper_record_only(self, paper_system):
        """paper config의 server는 paper record만 생성."""
        sys = paper_system
        r = sys["server"].call_tool(
            "propose_submit_order",
            payload={"symbol": "005930", "side": "BUY",
                     "qty": 1, "order_type": "MARKET"},
            requested_by="agent",
        )
        rec = sys["store"].get(r.result["approval_id"])
        assert rec.mode == "paper"

    def test_live_record_blocked_in_paper_gateway(
        self, paper_system, tmp_path
    ):
        """동일 DB에 live record가 있어도 paper gateway는 거부."""
        sys = paper_system
        # 직접 store에 live record 삽입 (악의적 시나리오 방어)
        aid = sys["store"].propose(
            action_kind="submit_order",
            payload={"symbol": "005930", "side": "BUY",
                     "qty": 1, "order_type": "MARKET"},
            requested_by="external_agent",
            mode="live",
        )
        sys["store"].approve(aid, decided_by="alice", allow_live=True)
        with pytest.raises(ModeViolationError):
            sys["gateway"].execute(aid)
        # 브로커 호출 없음
        assert len(sys["broker"].submit_log) == 0

    def test_live_system_routes_to_live_broker(self, live_system):
        sys = live_system
        r = sys["server"].call_tool(
            "propose_submit_order",
            payload={"symbol": "005930", "side": "BUY",
                     "qty": 1, "order_type": "MARKET"},
            requested_by="agent",
        )
        aid = r.result["approval_id"]
        sys["store"].approve(aid, decided_by="alice", allow_live=True)
        sys["gateway"].execute(aid)
        # live 어댑터에만 호출, paper에는 없음
        assert len(sys["live_broker"].submit_log) == 1
        assert len(sys["paper_broker"].submit_log) == 0


# ===========================================================================
# 6. Kill switch 활성 시 모든 execute 차단
# ===========================================================================

class TestKillSwitchE2E:
    def test_kill_switch_blocks_all_execute(self, paper_system):
        sys = paper_system

        # 2개 propose+approve
        ids = []
        for _ in range(2):
            r = sys["server"].call_tool(
                "propose_submit_order",
                payload={"symbol": "005930", "side": "BUY",
                         "qty": 1, "order_type": "MARKET"},
                requested_by="agent",
            )
            aid = r.result["approval_id"]
            sys["store"].approve(aid, decided_by="alice")
            ids.append(aid)

        # kill switch 활성
        sys["kill"].set(True)

        for aid in ids:
            with pytest.raises(KillSwitchActiveError):
                sys["gateway"].execute(aid)

        # 어떤 브로커 호출도 없음
        assert len(sys["broker"].submit_log) == 0

        # 비활성화 후 정상 동작
        sys["kill"].set(False)
        sys["gateway"].execute(ids[0])
        assert len(sys["broker"].submit_log) == 1


# ===========================================================================
# 7-8. TTL 만료
# ===========================================================================

class TestTTLE2E:
    def test_proposed_expires_after_5min(self, paper_system, monkeypatch):
        sys = paper_system
        r = sys["server"].call_tool(
            "propose_submit_order",
            payload={"symbol": "005930", "side": "BUY",
                     "qty": 1, "order_type": "MARKET"},
            requested_by="agent",
        )
        aid = r.result["approval_id"]

        # 시계 점프 +400초
        future = sys["store"]._now() + 400
        monkeypatch.setattr(sys["store"], "_now", lambda: future)

        # query 시 EXPIRED로 마킹됨
        rec = sys["store"].get(aid)
        assert rec.status == ApprovalStatus.EXPIRED

        # approve 거부
        with pytest.raises(InvalidTransitionError):
            sys["store"].approve(aid, decided_by="alice")

    def test_approved_expires_after_60s(self, paper_system, monkeypatch):
        sys = paper_system
        r = sys["server"].call_tool(
            "propose_submit_order",
            payload={"symbol": "005930", "side": "BUY",
                     "qty": 1, "order_type": "MARKET"},
            requested_by="agent",
        )
        aid = r.result["approval_id"]
        sys["store"].approve(aid, decided_by="alice")

        # +120초 점프
        future = sys["store"]._now() + 120
        monkeypatch.setattr(sys["store"], "_now", lambda: future)

        with pytest.raises(ExpiredApprovalError):
            sys["gateway"].execute(aid)
        assert len(sys["broker"].submit_log) == 0


# ===========================================================================
# 9. EXEC_FAILED 후 재실행 차단
# ===========================================================================

class TestFailureFlow:
    def test_exec_failed_blocks_retry(self, tmp_path):
        cfg = ServerConfig(
            approval_db_path=tmp_path / "approvals.sqlite",
            mode="paper", allow_live=False, project_root=tmp_path,
        )
        store = ApprovalStore(db_path=cfg.approval_db_path)
        bad_broker = MockKISBroker(mode="paper", fail_on_symbol="066570")
        gateway = ExecutionGateway(
            store=store, paper_broker=bad_broker, mode="paper",
        )
        handlers = WriteHandlers(store=store, mode="paper")
        server = RestrictedServer(
            config=cfg, store=store, gateway=gateway, handlers=handlers,
        )

        r = server.call_tool(
            "propose_submit_order",
            payload={"symbol": "066570", "side": "BUY",
                     "qty": 1, "order_type": "MARKET"},
            requested_by="agent",
        )
        aid = r.result["approval_id"]
        store.approve(aid, decided_by="alice")

        with pytest.raises(BrokerExecutionError):
            gateway.execute(aid)
        rec = store.get(aid)
        assert rec.status == ApprovalStatus.EXEC_FAILED
        assert "066570" in (rec.error_message or "")

        # 재실행 → AlreadyExecutedError
        with pytest.raises(AlreadyExecutedError):
            gateway.execute(aid)


# ===========================================================================
# 10. 동시 approve
# ===========================================================================

class TestConcurrentApprove:
    def test_two_operators_one_wins(self, paper_system):
        sys = paper_system
        r = sys["server"].call_tool(
            "propose_submit_order",
            payload={"symbol": "005930", "side": "BUY",
                     "qty": 1, "order_type": "MARKET"},
            requested_by="agent",
        )
        aid = r.result["approval_id"]

        success: list[str] = []
        errors: list[Exception] = []
        barrier = threading.Barrier(2)

        def approve_attempt(name):
            barrier.wait()
            try:
                sys["store"].approve(aid, decided_by=name)
                success.append(name)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=approve_attempt, args=("alice",))
        t2 = threading.Thread(target=approve_attempt, args=("bob",))
        t1.start(); t2.start()
        t1.join(); t2.join()

        assert len(success) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], InvalidTransitionError)


# ===========================================================================
# 11. 시크릿 누설 차단 종합 (defense in depth)
# ===========================================================================

class TestSecretIsolation:
    def test_no_secrets_anywhere_in_responses(self, paper_system):
        sys = paper_system
        r = sys["server"].call_tool(
            "propose_submit_order",
            payload={"symbol": "005930", "side": "BUY",
                     "qty": 1, "order_type": "MARKET"},
            requested_by="agent",
        )
        aid = r.result["approval_id"]
        sys["store"].approve(aid, decided_by="alice")
        sys["gateway"].execute(aid)

        q = sys["server"].call_tool("query_approval_status", approval_id=aid)
        snap = sys["server"].status_snapshot()
        gateway_snap = sys["gateway"].status_snapshot()

        for blob in (q.result, snap, gateway_snap):
            blob_str = str(blob).lower()
            for forbidden in ("password", "appsecret", "appkey", "private_key"):
                assert forbidden not in blob_str, (
                    f"forbidden keyword in response: {forbidden}"
                )


# ===========================================================================
# 12. mode 일치성 종합
# ===========================================================================

class TestModeConsistency:
    def test_paper_record_paper_gateway_paper_broker(self, paper_system):
        sys = paper_system
        r = sys["server"].call_tool(
            "propose_submit_order",
            payload={"symbol": "005930", "side": "BUY",
                     "qty": 1, "order_type": "MARKET"},
            requested_by="agent",
        )
        aid = r.result["approval_id"]
        sys["store"].approve(aid, decided_by="alice")
        sys["gateway"].execute(aid)

        # 모든 단계에서 paper
        rec = sys["store"].get(aid)
        assert rec.mode == "paper"
        assert sys["gateway"].mode == "paper"
        assert sys["broker"].mode == "paper"
        assert "PAPER" in sys["broker"].submit_log[0].get("symbol", "") or \
               sys["broker"]._counter > 0  # 호출 발생 확인

    def test_factory_consistency_paper(self, tmp_path):
        cfg = ServerConfig(
            approval_db_path=tmp_path / "approvals.sqlite",
            mode="paper", allow_live=False, project_root=tmp_path,
        )
        pb = MockKISBroker(mode="paper")
        server = build_restricted_server(config=cfg, paper_broker=pb)
        snap = server.status_snapshot()
        assert snap["mode"] == "paper"
        assert snap["gateway"]["mode"] == "paper"
        assert len(snap["tools"]) == 8
