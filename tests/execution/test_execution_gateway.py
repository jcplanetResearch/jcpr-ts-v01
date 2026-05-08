"""Tests for execution/execution_gateway.py — propose + execute + idempotency."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from src.brokers.base import (
    BrokerExecutionInterface,
    OrderRequest,
    OrderResponse,
    OrderSide,
    OrderStatus,
    OrderType,
)
from src.execution._approval_state import (
    ApprovalState,
    ApprovalStore,
    SelfApprovalError,
)
from src.execution.execution_gateway import (
    ACTION_CANCEL_ORDER,
    ACTION_PLACE_ORDER,
    ExecutionGateway,
    GatewayError,
    ProposalResult,
)


# =============================================================================
# Mock broker
# =============================================================================

class MockBrokerExecution(BrokerExecutionInterface):
    """Test double — records all calls, returns canned responses."""

    def __init__(self) -> None:
        self.place_orders: list[OrderRequest] = []
        self.cancel_orders: list[tuple[str, str]] = []
        self._next_response: OrderResponse | None = None
        self._raise: Exception | None = None

    def queue_response(self, response: OrderResponse) -> None:
        self._next_response = response

    def queue_error(self, exc: Exception) -> None:
        self._raise = exc

    def place_order(self, request: OrderRequest) -> OrderResponse:
        self.place_orders.append(request)
        if self._raise:
            exc = self._raise
            self._raise = None
            raise exc
        if self._next_response:
            r = self._next_response
            self._next_response = None
            return r
        # Default success response
        return OrderResponse(
            success=True,
            broker_order_id=f"broker-{len(self.place_orders)}",
            client_order_id=request.client_order_id,
            status=OrderStatus.PENDING,
            error_code=None,
            error_message=None,
            received_at_utc=datetime.now(tz=timezone.utc),
        )

    def cancel_order(self, *, broker_order_id: str,
                     approval_id: str) -> OrderResponse:
        self.cancel_orders.append((broker_order_id, approval_id))
        if self._raise:
            exc = self._raise
            self._raise = None
            raise exc
        return OrderResponse(
            success=True,
            broker_order_id=broker_order_id,
            client_order_id=f"cancel-{broker_order_id}",
            status=OrderStatus.CANCELLED,
            error_code=None,
            error_message=None,
            received_at_utc=datetime.now(tz=timezone.utc),
        )


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def utc_now() -> datetime:
    return datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path, utc_now):
    return ApprovalStore(
        db_path=tmp_path / "gw.db",
        _now_fn=lambda: utc_now,
    )


@pytest.fixture
def broker():
    return MockBrokerExecution()


@pytest.fixture
def gateway(broker, store, utc_now):
    return ExecutionGateway(
        broker=broker,
        store=store,
        agent_actor="trading_agent",
        _now_fn=lambda: utc_now,
    )


@pytest.fixture
def order_request(utc_now):
    return OrderRequest(
        symbol="005930",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("10"),
        limit_price_krw=Decimal("70000"),
        client_order_id="strat-001-20260507-001",
        strategy_id="strat-001",
        approval_id="placeholder",  # gateway overwrites
        requested_at_utc=utc_now,
    )


# =============================================================================
# Construction
# =============================================================================

class TestGatewayConstruction:
    def test_rejects_missing_broker(self, store):
        with pytest.raises(ValueError, match="broker"):
            ExecutionGateway(broker=None, store=store)

    def test_rejects_missing_store(self, broker):
        with pytest.raises(ValueError, match="store"):
            ExecutionGateway(broker=broker, store=None)

    def test_rejects_empty_actor(self, broker, store):
        with pytest.raises(ValueError, match="agent_actor"):
            ExecutionGateway(broker=broker, store=store, agent_actor="")


# =============================================================================
# Propose
# =============================================================================

class TestProposeOrder:
    def test_creates_proposed_state(self, gateway, order_request):
        result = gateway.propose_order(order_request=order_request)
        assert isinstance(result, ProposalResult)
        assert result.state == ApprovalState.PROPOSED
        assert result.approval_id.startswith("ap-")

    def test_does_not_call_broker(self, gateway, broker, order_request):
        gateway.propose_order(order_request=order_request)
        assert broker.place_orders == []  # NOT called

    def test_payload_persisted(self, gateway, store, order_request):
        result = gateway.propose_order(order_request=order_request)
        proposal = store.get(result.approval_id)
        assert proposal.payload["symbol"] == "005930"
        assert proposal.payload["side"] == "buy"
        assert proposal.payload["quantity"] == "10"
        assert proposal.payload["limit_price_krw"] == "70000"

    def test_uses_default_actor(self, gateway, store, order_request):
        result = gateway.propose_order(order_request=order_request)
        proposal = store.get(result.approval_id)
        assert proposal.requested_by == "trading_agent"

    def test_custom_actor(self, gateway, store, order_request):
        result = gateway.propose_order(
            order_request=order_request,
            requested_by="risk_agent",
        )
        proposal = store.get(result.approval_id)
        assert proposal.requested_by == "risk_agent"

    def test_interrupted_blocks(self, gateway, order_request):
        gateway.signal_interrupt()
        with pytest.raises(GatewayError, match="interrupted"):
            gateway.propose_order(order_request=order_request)


class TestProposeCancel:
    def test_creates_cancel_proposal(self, gateway, store):
        result = gateway.propose_cancel(broker_order_id="bo-123")
        proposal = store.get(result.approval_id)
        assert proposal.action_type == ACTION_CANCEL_ORDER
        assert proposal.payload["broker_order_id"] == "bo-123"

    def test_rejects_empty_id(self, gateway):
        with pytest.raises(ValueError, match="broker_order_id"):
            gateway.propose_cancel(broker_order_id="")


# =============================================================================
# Execute
# =============================================================================

class TestExecuteApproved:
    def test_full_workflow(self, gateway, broker, store, order_request):
        # Propose
        proposal_result = gateway.propose_order(order_request=order_request)
        # Approve (different actor than requested_by="trading_agent")
        store.transition(
            approval_id=proposal_result.approval_id,
            target_state=ApprovalState.APPROVED,
            actor="operator-1",
        )
        # Execute
        exec_result = gateway.execute_approved(
            approval_id=proposal_result.approval_id,
        )
        assert exec_result.success is True
        assert exec_result.broker_order_id is not None
        assert len(broker.place_orders) == 1
        assert broker.place_orders[0].symbol == "005930"

        # Verify state is EXECUTED
        final = store.get(proposal_result.approval_id)
        assert final.state == ApprovalState.EXECUTED

    def test_blocks_unapproved(self, gateway, store, order_request):
        proposal_result = gateway.propose_order(order_request=order_request)
        # State is PROPOSED — not APPROVED
        with pytest.raises(GatewayError, match="must be APPROVED"):
            gateway.execute_approved(approval_id=proposal_result.approval_id)

    def test_blocks_rejected(self, gateway, store, order_request):
        result = gateway.propose_order(order_request=order_request)
        store.transition(
            approval_id=result.approval_id,
            target_state=ApprovalState.REJECTED,
            actor="operator",
            reason_kr="too risky",
        )
        with pytest.raises(GatewayError, match="must be APPROVED"):
            gateway.execute_approved(approval_id=result.approval_id)

    def test_idempotent_on_executed(self, gateway, broker, store,
                                    order_request):
        result = gateway.propose_order(order_request=order_request)
        store.transition(
            approval_id=result.approval_id,
            target_state=ApprovalState.APPROVED,
            actor="operator",
        )
        # First execution
        first = gateway.execute_approved(approval_id=result.approval_id)
        assert first.success is True
        first_call_count = len(broker.place_orders)

        # Second execution — should NOT call broker again
        second = gateway.execute_approved(approval_id=result.approval_id)
        assert second.success is True
        assert second.broker_order_id == first.broker_order_id
        assert len(broker.place_orders) == first_call_count  # no extra call

    def test_broker_failure_marks_executed(self, gateway, broker, store,
                                           order_request):
        """A failed broker call still marks the approval EXECUTED with success=False.

        This prevents retry-storms — operator must propose a new order.
        """
        broker.queue_error(RuntimeError("broker timeout"))
        result = gateway.propose_order(order_request=order_request)
        store.transition(
            approval_id=result.approval_id,
            target_state=ApprovalState.APPROVED,
            actor="operator",
        )
        exec_result = gateway.execute_approved(approval_id=result.approval_id)
        assert exec_result.success is False
        assert exec_result.error_message == "broker timeout"
        # State must still be EXECUTED (not APPROVED — prevents retry)
        final = store.get(result.approval_id)
        assert final.state == ApprovalState.EXECUTED

    def test_cancel_workflow(self, gateway, broker, store):
        result = gateway.propose_cancel(broker_order_id="bo-existing")
        store.transition(
            approval_id=result.approval_id,
            target_state=ApprovalState.APPROVED,
            actor="operator",
        )
        exec_result = gateway.execute_approved(approval_id=result.approval_id)
        assert exec_result.success is True
        assert len(broker.cancel_orders) == 1
        assert broker.cancel_orders[0][0] == "bo-existing"

    def test_interrupted_blocks_execute(self, gateway, store, order_request):
        result = gateway.propose_order(order_request=order_request)
        store.transition(
            approval_id=result.approval_id,
            target_state=ApprovalState.APPROVED,
            actor="operator",
        )
        gateway.signal_interrupt()
        with pytest.raises(GatewayError, match="interrupted"):
            gateway.execute_approved(approval_id=result.approval_id)

    def test_rejects_empty_id(self, gateway):
        with pytest.raises(ValueError, match="approval_id"):
            gateway.execute_approved(approval_id="")


# =============================================================================
# Self-approval defense
# =============================================================================

class TestSelfApprovalDefense:
    def test_agent_cannot_approve_own_proposal(self, gateway, store,
                                               order_request):
        result = gateway.propose_order(
            order_request=order_request,
            requested_by="alice",
        )
        with pytest.raises(SelfApprovalError):
            store.transition(
                approval_id=result.approval_id,
                target_state=ApprovalState.APPROVED,
                actor="alice",
            )
