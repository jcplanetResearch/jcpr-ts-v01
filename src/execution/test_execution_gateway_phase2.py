"""Stage 2A unit tests for ExecutionGateway — operator-env version.

운영자 로컬 환경 기준: Phase 1 진짜 approval_store + Task 9 진짜 broker base 사용.
tests/_stubs.py shim에 의존하지 않음.
"""
from __future__ import annotations
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
import pytest

from src.execution.approval_store import (
    ActionKind, ApprovalState, ApprovalStore, ApprovalStoreError,
    ExpiredApprovalError, InvalidStateTransitionError, SelfApprovalError,
)
from src.execution.execution_gateway import (
    ExecutionGateway, ExecutionResult, GatewayError,
    InterruptedExecutionError, LiveModeBlockedError,
)


# ---------------------------------------------------------------------------
# In-test broker doubles
# ---------------------------------------------------------------------------
class _FakeOrderRequest:
    def __init__(self, symbol="005930", side="BUY", quantity=Decimal("10"),
                 order_type="LIMIT", limit_price=Decimal("75000"),
                 time_in_force="DAY", client_order_id="jcpr-test-001",
                 strategy_id="momentum_v1"):
        self.symbol = symbol; self.side = side; self.quantity = quantity
        self.order_type = order_type; self.limit_price = limit_price
        self.time_in_force = time_in_force; self.client_order_id = client_order_id
        self.strategy_id = strategy_id

class _FakeOrderResponse:
    def __init__(self, accepted=True, broker_order_id="B-12345",
                 client_order_id="jcpr-test-001", filled_quantity=Decimal("10"),
                 average_price=Decimal("75000"), error_code=None, error_message=None):
        self.accepted = accepted; self.broker_order_id = broker_order_id
        self.client_order_id = client_order_id; self.filled_quantity = filled_quantity
        self.average_price = average_price; self.error_code = error_code
        self.error_message = error_message
        self.submitted_at_utc = datetime.now(timezone.utc)

class _MockBroker:
    def __init__(self, *, accepted=True, broker_order_id="B-12345",
                 filled_quantity=Decimal("10"), average_price=Decimal("75000"),
                 error_code=None, error_message=None, raise_exception=None):
        self.accepted = accepted; self.broker_order_id = broker_order_id
        self.filled_quantity = filled_quantity; self.average_price = average_price
        self.error_code = error_code; self.error_message = error_message
        self.raise_exception = raise_exception
        self.place_order_calls = []; self.cancel_order_calls = []

    def place_order(self, request, *, approval_id):
        self.place_order_calls.append((request, approval_id))
        if self.raise_exception: raise self.raise_exception
        return _FakeOrderResponse(
            accepted=self.accepted,
            broker_order_id=self.broker_order_id if self.accepted else None,
            client_order_id=request.client_order_id,
            filled_quantity=self.filled_quantity if self.accepted else Decimal("0"),
            average_price=self.average_price if self.accepted else None,
            error_code=self.error_code, error_message=self.error_message,
        )
    def cancel_order(self, *, broker_order_id, symbol, approval_id):
        self.cancel_order_calls.append({"broker_order_id": broker_order_id})
        return {"cancelled": True}

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def store(tmp_path):
    s = ApprovalStore(db_path=tmp_path / "approvals.sqlite")
    yield s
    s.close()

@pytest.fixture
def broker(): return _MockBroker()

@pytest.fixture
def gateway(store, broker):
    return ExecutionGateway(approval_store=store, broker=broker, mode="paper")

@pytest.fixture
def order_req(): return _FakeOrderRequest()

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestGatewayConstruction:
    def test_default_paper_mode(self, store, broker):
        g = ExecutionGateway(approval_store=store, broker=broker)
        assert g.mode == "paper" and g.allow_live is False

    def test_live_without_allow_live_blocked(self, store, broker):
        with pytest.raises(LiveModeBlockedError):
            ExecutionGateway(approval_store=store, broker=broker, mode="live", allow_live=False)

    def test_live_with_allow_live_succeeds(self, store, broker):
        g = ExecutionGateway(approval_store=store, broker=broker, mode="live", allow_live=True)
        assert g.mode == "live" and g.allow_live is True

    def test_invalid_mode_rejected(self, store, broker):
        with pytest.raises(ValueError):
            ExecutionGateway(approval_store=store, broker=broker, mode="testnet")

    def test_store_property(self, gateway, store): assert gateway.store is store

class TestProposeOrder:
    def test_returns_approval_id(self, gateway, order_req):
        assert gateway.propose_order(order_req, requested_by="risk_agent").startswith("apv-")

    def test_creates_proposed_record(self, gateway, order_req, store):
        aid = gateway.propose_order(order_req, requested_by="market_agent")
        r = store.get(aid)
        assert r.state == ApprovalState.PROPOSED
        assert r.action_kind == ActionKind.SUBMIT_ORDER
        assert r.requested_by == "market_agent"

    def test_serializes_decimal_to_string(self, gateway, order_req, store):
        aid = gateway.propose_order(order_req, requested_by="market_agent")
        r = store.get(aid)
        assert r.action_payload["quantity"] == "10"
        assert r.action_payload["limit_price"] == "75000"

    def test_requires_requester(self, gateway, order_req):
        with pytest.raises(ValueError):
            gateway.propose_order(order_req, requested_by="")

    def test_custom_ttl(self, gateway, order_req, store):
        aid = gateway.propose_order(order_req, requested_by="market_agent", ttl_seconds=600)
        r = store.get(aid)
        ttl = (r.expires_at_utc - r.created_at_utc).total_seconds()
        assert 599 <= ttl <= 601

class TestExecuteApproved:
    def test_happy_path(self, gateway, order_req, store, broker):
        aid = gateway.propose_order(order_req, requested_by="market_agent")
        store.approve(aid, decided_by="operator-jcpr")
        result = gateway.execute_approved(aid, actor="operator-jcpr")
        assert result.success is True
        assert result.state == ApprovalState.EXECUTED
        assert result.broker_order_id == "B-12345"
        assert result.filled_quantity == Decimal("10")
        assert len(broker.place_order_calls) == 1

    def test_executed_persisted(self, gateway, order_req, store):
        aid = gateway.propose_order(order_req, requested_by="market_agent")
        store.approve(aid, decided_by="operator-jcpr")
        gateway.execute_approved(aid, actor="operator-jcpr")
        assert store.get(aid).state == ApprovalState.EXECUTED

    def test_broker_rejection(self, store, order_req):
        b = _MockBroker(accepted=False, error_message="cash balance below required")
        gw = ExecutionGateway(approval_store=store, broker=b)
        aid = gw.propose_order(order_req, requested_by="market_agent")
        store.approve(aid, decided_by="operator-jcpr")
        result = gw.execute_approved(aid, actor="operator-jcpr")
        assert result.success is False
        assert result.state == ApprovalState.EXEC_FAILED

    def test_broker_exception(self, store, order_req):
        b = _MockBroker(raise_exception=RuntimeError("KIS endpoint timeout"))
        gw = ExecutionGateway(approval_store=store, broker=b)
        aid = gw.propose_order(order_req, requested_by="market_agent")
        store.approve(aid, decided_by="operator-jcpr")
        with pytest.raises(GatewayError, match="KIS endpoint timeout"):
            gw.execute_approved(aid, actor="operator-jcpr")
        assert store.get(aid).state == ApprovalState.EXEC_FAILED

    def test_idempotent_executed(self, gateway, order_req, store, broker):
        aid = gateway.propose_order(order_req, requested_by="market_agent")
        store.approve(aid, decided_by="operator-jcpr")
        gateway.execute_approved(aid, actor="operator-jcpr")
        gateway.execute_approved(aid, actor="operator-jcpr")
        assert len(broker.place_order_calls) == 1

    def test_idempotent_failed(self, store, order_req):
        b = _MockBroker(accepted=False, error_message="rejected")
        gw = ExecutionGateway(approval_store=store, broker=b)
        aid = gw.propose_order(order_req, requested_by="market_agent")
        store.approve(aid, decided_by="operator-jcpr")
        gw.execute_approved(aid, actor="operator-jcpr")
        second = gw.execute_approved(aid, actor="operator-jcpr")
        assert len(b.place_order_calls) == 1
        assert second.state == ApprovalState.EXEC_FAILED

    def test_unknown_id_raises(self, gateway):
        with pytest.raises(GatewayError, match="not found"):
            gateway.execute_approved("apv-99999999-deadbeef", actor="op")

    def test_actor_required(self, gateway, order_req, store):
        aid = gateway.propose_order(order_req, requested_by="market_agent")
        store.approve(aid, decided_by="operator-jcpr")
        with pytest.raises(ValueError):
            gateway.execute_approved(aid, actor="")

    def test_mode_mismatch_raises(self, store, order_req):
        b = _MockBroker()
        paper_gw = ExecutionGateway(approval_store=store, broker=b, mode="paper")
        aid = paper_gw.propose_order(order_req, requested_by="market_agent")
        store.approve(aid, decided_by="operator-jcpr")
        live_gw = ExecutionGateway(approval_store=store, broker=b, mode="live", allow_live=True)
        with pytest.raises(GatewayError, match="mode"):
            live_gw.execute_approved(aid, actor="operator-jcpr")

class TestInterruptHandling:
    def test_interrupt_at_propose(self, store, broker, order_req):
        flag = {"v": False}
        gw = ExecutionGateway(approval_store=store, broker=broker, interrupt_check=lambda: flag["v"])
        flag["v"] = True
        with pytest.raises(InterruptedExecutionError):
            gw.propose_order(order_req, requested_by="market_agent")

    def test_interrupt_before_mark_executing_leaves_approved(self, store, broker, order_req):
        flag = {"v": False}
        gw = ExecutionGateway(approval_store=store, broker=broker, interrupt_check=lambda: flag["v"])
        aid = gw.propose_order(order_req, requested_by="market_agent")
        store.approve(aid, decided_by="operator-jcpr")
        flag["v"] = True
        with pytest.raises(InterruptedExecutionError):
            gw.execute_approved(aid, actor="operator-jcpr")
        assert store.get(aid).state == ApprovalState.APPROVED

    def test_interrupt_during_broker_call_marks_exec_failed(self, store, order_req):
        b = _MockBroker()
        gw = ExecutionGateway(approval_store=store, broker=b, interrupt_check=lambda: False)
        aid = gw.propose_order(order_req, requested_by="market_agent")
        store.approve(aid, decided_by="operator-jcpr")
        count = {"n": 0}
        def staged():
            count["n"] += 1
            return count["n"] >= 2
        gw._interrupt_check = staged
        with pytest.raises(InterruptedExecutionError):
            gw.execute_approved(aid, actor="operator-jcpr")
        assert store.get(aid).state == ApprovalState.EXEC_FAILED
        assert "interrupted" in store.get(aid).error_message
        assert len(b.place_order_calls) == 0

class TestCancelProposed:
    def test_cancel_pending_succeeds(self, gateway, order_req, store):
        aid = gateway.propose_order(order_req, requested_by="market_agent")
        gateway.cancel_proposed(aid, actor="market_agent", reason="changed mind")
        assert store.get(aid).state == ApprovalState.CANCELLED

    def test_cancel_after_approve_raises(self, gateway, order_req, store):
        aid = gateway.propose_order(order_req, requested_by="market_agent")
        store.approve(aid, decided_by="operator-jcpr")
        with pytest.raises(Exception):
            gateway.cancel_proposed(aid, actor="market_agent")
