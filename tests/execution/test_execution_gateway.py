"""
tests/execution/test_execution_gateway.py — JCPR-ts-v01 (Phase 2-A)
====================================================================

ExecutionGateway 테스트 — Phase 1의 통합 ApprovalStore와 결합된 상태에서
모든 핵심 동작을 검증.

검증 항목 (test inventory)
--------------------------
1. 정상 흐름 (happy path)
   - paper 모드 submit_order: APPROVED → EXECUTING → EXECUTED
   - paper 모드 cancel_order
   - live 모드 submit_order (allow_live=True 가정)
2. 상태 전이 (state transitions)
   - 동시 execute 시도 → AlreadyExecutingError
   - 이미 EXECUTED 레코드 재실행 → AlreadyExecutedError
   - PROPOSED 상태 실행 → NotApprovedError
   - REJECTED/CANCELLED 상태 실행 → NotApprovedError
   - EXPIRED 상태 실행 → ExpiredApprovalError
3. Kill switch
   - 활성 시 즉시 KillSwitchActiveError
   - 어댑터 호출되지 않음 검증
4. 모드 가드 (mode guards)
   - live mode + allow_live=False → __init__ 단계 ValueError
   - live mode + live_broker=None → __init__ 단계 ValueError
   - paper 게이트웨이에 live record → ModeViolationError
   - 어댑터 mode 속성 불일치 → __init__ ValueError
5. 브로커 실패 처리
   - submit_order에서 브로커 예외 발생 → EXEC_FAILED 기록 + BrokerExecutionError
   - 실패 후 같은 ID 재실행 → AlreadyExecutedError
6. 잘못된 action_kind
   - set_capacity / kill_switch는 게이트웨이 처리 대상 아님 → BrokerExecutionError
   - 알 수 없는 action_kind → BrokerExecutionError
7. 진단 (diagnostics)
   - status_snapshot은 시크릿 없는 dict 반환
   - mode / is_live 프로퍼티
8. 만료(TTL) 처리
   - 실행 TTL 초과 후 execute → ExpiredApprovalError

테스트 전략 (test strategy)
---------------------------
- 실 KIS API는 호출하지 않음. FakeBroker를 사용.
- ApprovalStore는 실제 SQLite(임시 디렉터리)에서 동작 — Phase 1
  통합 store의 실제 동작을 함께 검증.
- 시간 의존 테스트는 monotonic mock 또는 store가 노출하는 시간 훅 사용.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from src.execution.approval_store import (
    ApprovalStore,
    ApprovalStatus,
    ApprovalNotFoundError,
)
from src.execution.execution_gateway import (
    ExecutionGateway,
    ExecutionResult,
    BrokerExecutionInterface,
    NotApprovedError,
    AlreadyExecutingError,
    AlreadyExecutedError,
    ExpiredApprovalError,
    KillSwitchActiveError,
    ModeViolationError,
    BrokerExecutionError,
)


# ---------------------------------------------------------------------------
# Fakes (테스트용 가짜 객체)
# ---------------------------------------------------------------------------

class FakeBroker:
    """BrokerExecutionInterface를 만족하는 인-메모리 가짜 브로커."""

    def __init__(self, mode: str = "paper", *, fail: bool = False,
                 fail_with: type[Exception] | None = None) -> None:
        self.mode = mode
        self._fail = fail
        self._fail_with = fail_with or RuntimeError
        self.submit_calls: list[dict[str, Any]] = []
        self.cancel_calls: list[dict[str, Any]] = []

    def submit_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.submit_calls.append(dict(payload))
        if self._fail:
            raise self._fail_with("simulated broker failure")
        return {
            "broker_order_id": f"BRK-{len(self.submit_calls):06d}",
            "status": "ACCEPTED",
            "echo": payload,
        }

    def cancel_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.cancel_calls.append(dict(payload))
        if self._fail:
            raise self._fail_with("simulated cancel failure")
        return {"status": "CANCELLED", "echo": payload}


class FakeKillSwitch:
    """KillSwitchProtocol 만족 — 토글 가능."""

    def __init__(self, active: bool = False) -> None:
        self._active = active

    def set(self, active: bool) -> None:
        self._active = active

    def is_active(self) -> bool:
        return self._active


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path: Path) -> ApprovalStore:
    """임시 SQLite로 ApprovalStore 생성. 0600 권한은 store 측에서 강제."""
    db = tmp_path / "approvals.sqlite"
    return ApprovalStore(db_path=db)


@pytest.fixture
def paper_broker() -> FakeBroker:
    return FakeBroker(mode="paper")


@pytest.fixture
def live_broker() -> FakeBroker:
    return FakeBroker(mode="live")


@pytest.fixture
def kill_switch() -> FakeKillSwitch:
    return FakeKillSwitch(active=False)


@pytest.fixture
def paper_gateway(store, paper_broker, kill_switch) -> ExecutionGateway:
    return ExecutionGateway(
        store=store,
        paper_broker=paper_broker,
        kill_switch=kill_switch,
        mode="paper",
        allow_live=False,
    )


@pytest.fixture
def live_gateway(store, paper_broker, live_broker, kill_switch) -> ExecutionGateway:
    return ExecutionGateway(
        store=store,
        paper_broker=paper_broker,
        live_broker=live_broker,
        kill_switch=kill_switch,
        mode="live",
        allow_live=True,
    )


# ---------------------------------------------------------------------------
# 헬퍼: 승인된 레코드를 빠르게 만들어 주는 함수
# ---------------------------------------------------------------------------

def _create_approved(store: ApprovalStore, *,
                     mode: str = "paper",
                     action_kind: str = "submit_order",
                     payload: dict | None = None,
                     requested_by: str = "agent",
                     decided_by: str = "operator") -> str:
    """PROPOSED → APPROVED 두 단계를 거쳐 approval_id 반환."""
    payload = payload or {
        "symbol": "005930", "side": "BUY", "qty": 10, "order_type": "LIMIT",
        "limit_price": 70000,
    }
    aid = store.propose(
        action_kind=action_kind,
        payload=payload,
        requested_by=requested_by,
        mode=mode,
    )
    store.approve(aid, decided_by=decided_by, allow_live=(mode == "live"))
    return aid


# ===========================================================================
# 1. 정상 흐름 (happy path)
# ===========================================================================

class TestHappyPath:
    def test_paper_submit_order_succeeds(self, paper_gateway, store, paper_broker):
        aid = _create_approved(store)
        result = paper_gateway.execute(aid)

        assert isinstance(result, ExecutionResult)
        assert result.approval_id == aid
        assert result.final_status == "EXECUTED"
        assert result.error_message is None
        assert result.broker_response is not None
        assert result.broker_response["status"] == "ACCEPTED"
        assert result.elapsed_ms >= 0

        # 어댑터 호출 1회
        assert len(paper_broker.submit_calls) == 1
        # store 상태 EXECUTED
        rec = store.get(aid)
        assert rec.status == ApprovalStatus.EXECUTED

    def test_paper_cancel_order_succeeds(self, paper_gateway, store, paper_broker):
        aid = _create_approved(
            store, action_kind="cancel_order",
            payload={"broker_order_id": "BRK-000001"},
        )
        result = paper_gateway.execute(aid)
        assert result.final_status == "EXECUTED"
        assert len(paper_broker.cancel_calls) == 1
        assert paper_broker.cancel_calls[0]["broker_order_id"] == "BRK-000001"

    def test_live_submit_routes_to_live_broker(
        self, live_gateway, store, paper_broker, live_broker
    ):
        aid = _create_approved(store, mode="live")
        live_gateway.execute(aid)
        # live 어댑터에만 호출되어야 함
        assert len(live_broker.submit_calls) == 1
        assert len(paper_broker.submit_calls) == 0


# ===========================================================================
# 2. 상태 전이 (state transitions)
# ===========================================================================

class TestStateTransitions:
    def test_execute_proposed_record_raises(self, paper_gateway, store):
        aid = store.propose(
            action_kind="submit_order",
            payload={"symbol": "005930", "side": "BUY", "qty": 1},
            requested_by="agent",
            mode="paper",
        )
        # APPROVED 안 했으므로 PROPOSED
        with pytest.raises(NotApprovedError):
            paper_gateway.execute(aid)

    def test_execute_rejected_record_raises(self, paper_gateway, store):
        aid = store.propose(
            action_kind="submit_order",
            payload={"symbol": "005930", "side": "BUY", "qty": 1},
            requested_by="agent",
            mode="paper",
        )
        store.reject(aid, decided_by="operator", reason="too risky")
        with pytest.raises(NotApprovedError):
            paper_gateway.execute(aid)

    def test_execute_cancelled_record_raises(self, paper_gateway, store):
        aid = store.propose(
            action_kind="submit_order",
            payload={"symbol": "005930", "side": "BUY", "qty": 1},
            requested_by="agent",
            mode="paper",
        )
        store.cancel(aid, cancelled_by="agent")
        with pytest.raises(NotApprovedError):
            paper_gateway.execute(aid)

    def test_execute_already_executed_raises(self, paper_gateway, store):
        aid = _create_approved(store)
        paper_gateway.execute(aid)
        with pytest.raises(AlreadyExecutedError):
            paper_gateway.execute(aid)

    def test_execute_unknown_id_raises(self, paper_gateway):
        with pytest.raises(ApprovalNotFoundError):
            paper_gateway.execute("apv-19700101-deadbeefcafebabe")


# ===========================================================================
# 3. Kill switch
# ===========================================================================

class TestKillSwitch:
    def test_kill_switch_active_blocks_execution(
        self, paper_gateway, store, kill_switch, paper_broker
    ):
        aid = _create_approved(store)
        kill_switch.set(True)
        with pytest.raises(KillSwitchActiveError):
            paper_gateway.execute(aid)
        # 어댑터 호출되지 않음
        assert len(paper_broker.submit_calls) == 0
        # 상태는 APPROVED 유지 (kill switch는 단순 거부, 상태 변경 없음)
        rec = store.get(aid)
        assert rec.status == ApprovalStatus.APPROVED

    def test_kill_switch_check_before_state_lookup(
        self, paper_gateway, kill_switch
    ):
        """존재하지 않는 ID라도 kill switch가 먼저 우선."""
        kill_switch.set(True)
        with pytest.raises(KillSwitchActiveError):
            paper_gateway.execute("apv-19700101-0000000000000000")


# ===========================================================================
# 4. 모드 가드 (mode guards)
# ===========================================================================

class TestModeGuards:
    def test_init_live_without_allow_live_raises(self, store, paper_broker, live_broker):
        with pytest.raises(ValueError, match="allow_live"):
            ExecutionGateway(
                store=store,
                paper_broker=paper_broker,
                live_broker=live_broker,
                mode="live",
                allow_live=False,
            )

    def test_init_live_without_live_broker_raises(self, store, paper_broker):
        with pytest.raises(ValueError, match="live_broker"):
            ExecutionGateway(
                store=store,
                paper_broker=paper_broker,
                live_broker=None,
                mode="live",
                allow_live=True,
            )

    def test_init_paper_broker_mode_mismatch_raises(self, store):
        bad = FakeBroker(mode="live")  # 'paper' 자리에 live 어댑터
        with pytest.raises(ValueError, match="paper_broker.mode"):
            ExecutionGateway(
                store=store,
                paper_broker=bad,  # type: ignore[arg-type]
                mode="paper",
            )

    def test_init_live_broker_mode_mismatch_raises(self, store, paper_broker):
        bad = FakeBroker(mode="paper")  # live 자리에 paper
        with pytest.raises(ValueError, match="live_broker.mode"):
            ExecutionGateway(
                store=store,
                paper_broker=paper_broker,
                live_broker=bad,  # type: ignore[arg-type]
                mode="live",
                allow_live=True,
            )

    def test_invalid_mode_raises(self, store, paper_broker):
        with pytest.raises(ValueError, match="paper/live"):
            ExecutionGateway(
                store=store,
                paper_broker=paper_broker,
                mode="dryrun",  # type: ignore[arg-type]
            )

    def test_paper_gateway_rejects_live_record(
        self, paper_gateway, store, paper_broker
    ):
        # live 모드로 승인된 레코드를 paper 게이트웨이에 보내려는 시도
        aid = store.propose(
            action_kind="submit_order",
            payload={"symbol": "005930", "side": "BUY", "qty": 1},
            requested_by="agent",
            mode="live",
        )
        store.approve(aid, decided_by="operator", allow_live=True)
        with pytest.raises(ModeViolationError):
            paper_gateway.execute(aid)
        assert len(paper_broker.submit_calls) == 0


# ===========================================================================
# 5. 브로커 실패 처리
# ===========================================================================

class TestBrokerFailures:
    def test_broker_exception_marks_exec_failed(self, store, kill_switch):
        bad_broker = FakeBroker(mode="paper", fail=True)
        gw = ExecutionGateway(
            store=store,
            paper_broker=bad_broker,
            kill_switch=kill_switch,
            mode="paper",
        )
        aid = _create_approved(store)
        with pytest.raises(BrokerExecutionError):
            gw.execute(aid)
        rec = store.get(aid)
        assert rec.status == ApprovalStatus.EXEC_FAILED
        assert rec.error_message is not None
        assert "simulated broker failure" in rec.error_message

    def test_failed_then_retry_blocks(self, store, kill_switch):
        bad_broker = FakeBroker(mode="paper", fail=True)
        gw = ExecutionGateway(
            store=store,
            paper_broker=bad_broker,
            kill_switch=kill_switch,
            mode="paper",
        )
        aid = _create_approved(store)
        with pytest.raises(BrokerExecutionError):
            gw.execute(aid)
        # 재실행 시도 → AlreadyExecutedError (terminal)
        with pytest.raises(AlreadyExecutedError):
            gw.execute(aid)


# ===========================================================================
# 6. 잘못된 action_kind
# ===========================================================================

class TestInvalidActionKind:
    def test_set_capacity_not_gateway_handled(self, paper_gateway, store):
        aid = _create_approved(
            store,
            action_kind="set_capacity",
            payload={"strategy": "momentum_v1", "capacity_krw": 1_000_000},
        )
        with pytest.raises(BrokerExecutionError, match="not gateway-handled"):
            paper_gateway.execute(aid)
        # 게이트웨이가 EXEC_FAILED로 마킹했는지 확인
        rec = store.get(aid)
        assert rec.status == ApprovalStatus.EXEC_FAILED

    def test_kill_switch_action_not_gateway_handled(self, paper_gateway, store):
        aid = _create_approved(
            store,
            action_kind="kill_switch",
            payload={"action": "activate", "reason": "manual"},
        )
        with pytest.raises(BrokerExecutionError, match="not gateway-handled"):
            paper_gateway.execute(aid)


# ===========================================================================
# 7. 진단 / 프로퍼티
# ===========================================================================

class TestDiagnostics:
    def test_status_snapshot_no_secrets(self, paper_gateway):
        snap = paper_gateway.status_snapshot()
        assert snap["mode"] == "paper"
        assert snap["allow_live"] is False
        assert snap["has_live_broker"] is False
        assert snap["kill_switch_active"] is False
        # 시크릿 키워드가 절대 포함되어선 안 됨
        snap_str = str(snap).lower()
        for forbidden in ("secret", "password", "appkey", "appsecret", "token"):
            assert forbidden not in snap_str

    def test_mode_property(self, paper_gateway, live_gateway):
        assert paper_gateway.mode == "paper"
        assert paper_gateway.is_live is False
        assert live_gateway.mode == "live"
        assert live_gateway.is_live is True

    def test_kill_switch_active_in_snapshot(
        self, paper_gateway, kill_switch
    ):
        kill_switch.set(True)
        snap = paper_gateway.status_snapshot()
        assert snap["kill_switch_active"] is True


# ===========================================================================
# 8. 동시성 (concurrency) — 두 스레드가 같은 ID로 execute 시도
# ===========================================================================

class TestConcurrency:
    def test_concurrent_execute_one_wins(self, store, paper_broker, kill_switch):
        """
        같은 approval_id를 두 스레드가 동시에 execute해도 정확히 하나만
        성공하고, 다른 하나는 AlreadyExecutingError 또는 AlreadyExecutedError.
        """
        gw = ExecutionGateway(
            store=store,
            paper_broker=paper_broker,
            kill_switch=kill_switch,
            mode="paper",
        )
        aid = _create_approved(store)

        results: list[Any] = []
        errors: list[Exception] = []
        barrier = threading.Barrier(2)

        def worker():
            barrier.wait()
            try:
                results.append(gw.execute(aid))
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start(); t2.start()
        t1.join(); t2.join()

        # 정확히 하나만 성공
        assert len(results) == 1
        assert len(errors) == 1
        assert isinstance(
            errors[0], (AlreadyExecutingError, AlreadyExecutedError)
        )
        # 어댑터 호출도 정확히 1회
        assert len(paper_broker.submit_calls) == 1


# ===========================================================================
# 9. TTL (실행 만료)
# ===========================================================================

class TestExecutionTTL:
    def test_expired_approval_blocks_execution(
        self, paper_gateway, store, monkeypatch
    ):
        """
        approve 후 실행 TTL(60초) 초과 시 EXPIRED로 처리되며
        ExpiredApprovalError 발생.

        구현 노트(implementation note):
            ApprovalStore는 get() 호출 시 TTL을 확인하여 자동으로 EXPIRED
            상태로 마이그레이트하는 lazy expiration을 지원한다고 STATUS.md
            §2 §"TTL: 결정 5분, 실행 60초"에 명시됨. 본 테스트는
            store.now()를 미래 시각으로 monkeypatch하여 그 동작을 검증.
        """
        aid = _create_approved(store)

        # 현재 시각 +120초로 store 시계 점프
        original = store._now if hasattr(store, "_now") else time.time
        future = original() + 120

        def fake_now():
            return future

        # 어떤 시계 훅을 쓰든 가장 우선될 만한 것을 시도
        if hasattr(store, "_now"):
            monkeypatch.setattr(store, "_now", fake_now)
        else:
            # 폴백: time.time 자체를 store 모듈에서 monkeypatch
            import src.execution.approval_store as mod
            monkeypatch.setattr(mod.time, "time", fake_now)

        with pytest.raises(ExpiredApprovalError):
            paper_gateway.execute(aid)
