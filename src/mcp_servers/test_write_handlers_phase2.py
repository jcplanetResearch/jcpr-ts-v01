"""Stage 2A unit tests for WriteHandlers — operator-env version.
Phase 1 진짜 approval_store 직접 사용. tests/_stubs.py shim 불필요.
"""
from __future__ import annotations
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
import pytest

from src.execution.approval_store import ApprovalState, ApprovalStore, SelfApprovalError
from src.execution._action_kind import ActionKind
from src.execution.execution_gateway import ExecutionGateway
from src.mcp_servers._write_handlers import WriteHandlerError, WriteHandlers, build_handlers


# ---------------------------------------------------------------------------
# In-test broker doubles (identical to gateway test doubles)
# ---------------------------------------------------------------------------
class _FakeOrderResponse:
    def __init__(self, accepted=True, broker_order_id="B-12345",
                 client_order_id=None, filled_quantity=Decimal("10"),
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
            client_order_id=getattr(request, "client_order_id", None),
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
def handlers(store, gateway):
    return build_handlers(store=store, gateway=gateway, operator_id="operator-jcpr")

# ---------------------------------------------------------------------------
# build_handlers guards
# ---------------------------------------------------------------------------
class TestBuildHandlers:
    def test_requires_store(self, gateway):
        with pytest.raises(ValueError):
            build_handlers(store=None, gateway=gateway, operator_id="op")

    def test_requires_gateway(self, store):
        with pytest.raises(ValueError):
            build_handlers(store=store, gateway=None, operator_id="op")

    def test_requires_operator_id(self, store, gateway):
        with pytest.raises(ValueError):
            build_handlers(store=store, gateway=gateway, operator_id="")

    def test_rejects_mismatched_store(self, store, broker, tmp_path):
        other_store = ApprovalStore(db_path=tmp_path / "other.sqlite")
        gw_other = ExecutionGateway(approval_store=other_store, broker=broker)
        with pytest.raises(ValueError, match="unified-store"):
            build_handlers(store=store, gateway=gw_other, operator_id="op")
        other_store.close()

# ---------------------------------------------------------------------------
# request_submit_order
# ---------------------------------------------------------------------------
class TestRequestSubmitOrder:
    def test_happy_path_limit_order(self, handlers, store):
        r = handlers.request_submit_order(
            symbol="005930", side="BUY", quantity="10", order_type="LIMIT",
            limit_price="75000", requested_by="market_agent",
        )
        assert r["state"] == "PROPOSED"
        assert r["action_kind"] == "submit_order"
        assert store.get(r["approval_id"]).action_payload["symbol"] == "005930"

    def test_market_order_no_limit_price(self, handlers):
        r = handlers.request_submit_order(
            symbol="005930", side="SELL", quantity="5",
            order_type="MARKET", requested_by="market_agent",
        )
        assert r["state"] == "PROPOSED"

    def test_limit_order_requires_limit_price(self, handlers):
        with pytest.raises(WriteHandlerError, match="LIMIT order requires"):
            handlers.request_submit_order(
                symbol="005930", side="BUY", quantity="10",
                order_type="LIMIT", requested_by="market_agent",
            )

    def test_invalid_side_rejected(self, handlers):
        with pytest.raises(WriteHandlerError, match="side must be"):
            handlers.request_submit_order(
                symbol="005930", side="HOLD", quantity="10",
                order_type="MARKET", requested_by="market_agent",
            )

    def test_zero_quantity_rejected(self, handlers):
        with pytest.raises(WriteHandlerError, match="quantity must be"):
            handlers.request_submit_order(
                symbol="005930", side="BUY", quantity="0",
                order_type="MARKET", requested_by="market_agent",
            )

    def test_invalid_decimal_rejected(self, handlers):
        with pytest.raises(WriteHandlerError, match="invalid quantity"):
            handlers.request_submit_order(
                symbol="005930", side="BUY", quantity="abc",
                order_type="MARKET", requested_by="market_agent",
            )

    def test_generic_actor_rejected(self, handlers):
        with pytest.raises(WriteHandlerError, match="too generic"):
            handlers.request_submit_order(
                symbol="005930", side="BUY", quantity="10",
                order_type="MARKET", requested_by="agent",
            )

    def test_empty_actor_rejected(self, handlers):
        with pytest.raises(WriteHandlerError, match="required"):
            handlers.request_submit_order(
                symbol="005930", side="BUY", quantity="10",
                order_type="MARKET", requested_by="",
            )

    def test_auto_generated_client_order_id(self, handlers, store):
        r = handlers.request_submit_order(
            symbol="005930", side="BUY", quantity="10",
            order_type="MARKET", requested_by="market_agent",
        )
        assert store.get(r["approval_id"]).action_payload["client_order_id"].startswith("jcpr-")

# ---------------------------------------------------------------------------
# Other request handlers
# ---------------------------------------------------------------------------
class TestOtherRequestHandlers:
    def test_cancel_order_creates_proposal(self, handlers, store):
        r = handlers.request_cancel_order(
            broker_order_id="B-12345", symbol="005930", requested_by="risk_agent",
        )
        assert store.get(r["approval_id"]).action_kind == ActionKind.CANCEL_ORDER

    def test_cancel_order_requires_broker_id(self, handlers):
        with pytest.raises(WriteHandlerError):
            handlers.request_cancel_order(broker_order_id="", symbol="005930", requested_by="risk_agent")

    def test_set_capacity_happy_path(self, handlers, store):
        r = handlers.request_set_capacity(
            new_capacity_krw="50000000",
            rationale="weekly capacity expansion ladder",
            requested_by="risk_agent",
        )
        assert store.get(r["approval_id"]).action_kind == ActionKind.SET_CAPACITY

    def test_set_capacity_negative_rejected(self, handlers):
        with pytest.raises(WriteHandlerError, match="non-negative"):
            handlers.request_set_capacity(
                new_capacity_krw="-1000", rationale="test rationale here", requested_by="risk_agent",
            )

    def test_set_capacity_short_rationale_rejected(self, handlers):
        with pytest.raises(WriteHandlerError, match="at least 10"):
            handlers.request_set_capacity(
                new_capacity_krw="10000", rationale="x", requested_by="risk_agent",
            )

    def test_kill_switch_happy_path(self, handlers, store):
        r = handlers.request_kill_switch(reason="anomaly detected", requested_by="risk_agent")
        assert store.get(r["approval_id"]).action_kind == ActionKind.KILL_SWITCH

    def test_kill_switch_short_reason_rejected(self, handlers):
        with pytest.raises(WriteHandlerError, match="at least 5"):
            handlers.request_kill_switch(reason="ok", requested_by="risk_agent")

# ---------------------------------------------------------------------------
# List / get / cancel
# ---------------------------------------------------------------------------
class TestListAndGet:
    def test_list_empty(self, handlers):
        r = handlers.list_pending_approvals()
        assert r["count"] == 0

    def test_list_returns_pending(self, handlers):
        handlers.request_submit_order(
            symbol="005930", side="BUY", quantity="10", order_type="MARKET", requested_by="market_agent"
        )
        handlers.request_kill_switch(reason="manual test", requested_by="risk_agent")
        assert handlers.list_pending_approvals()["count"] == 2

    def test_list_filtered_by_requester(self, handlers):
        handlers.request_submit_order(
            symbol="005930", side="BUY", quantity="10", order_type="MARKET", requested_by="market_agent"
        )
        handlers.request_kill_switch(reason="manual test", requested_by="risk_agent")
        assert handlers.list_pending_approvals(requested_by="market_agent")["count"] == 1

    def test_get_detail_returns_payload(self, handlers):
        r = handlers.request_submit_order(
            symbol="005930", side="BUY", quantity="10", order_type="MARKET", requested_by="market_agent"
        )
        d = handlers.get_approval_detail(approval_id=r["approval_id"])
        assert d["action_payload"]["symbol"] == "005930"

    def test_get_detail_unknown_raises(self, handlers):
        with pytest.raises(WriteHandlerError, match="not found"):
            handlers.get_approval_detail(approval_id="apv-99999999-deadbeef")

    def test_cancel_proposed_by_requester(self, handlers, store):
        r = handlers.request_submit_order(
            symbol="005930", side="BUY", quantity="10", order_type="MARKET", requested_by="market_agent"
        )
        result = handlers.cancel_proposed_action(approval_id=r["approval_id"], actor="market_agent")
        assert result["state"] == "CANCELLED"

    def test_cancel_by_third_party_blocked(self, handlers):
        r = handlers.request_submit_order(
            symbol="005930", side="BUY", quantity="10", order_type="MARKET", requested_by="market_agent"
        )
        with pytest.raises(WriteHandlerError, match="requester or the operator"):
            handlers.cancel_proposed_action(approval_id=r["approval_id"], actor="risk_agent")

    def test_cancel_by_operator_allowed(self, handlers):
        r = handlers.request_submit_order(
            symbol="005930", side="BUY", quantity="10", order_type="MARKET", requested_by="market_agent"
        )
        result = handlers.cancel_proposed_action(approval_id=r["approval_id"], actor="operator-jcpr")
        assert result["state"] == "CANCELLED"

# ---------------------------------------------------------------------------
# execute_approved_action — Phase 2 핵심
# ---------------------------------------------------------------------------
class TestExecuteApprovedAction:
    def test_submit_order_dispatches_to_gateway(self, handlers, store, broker):
        r = handlers.request_submit_order(
            symbol="005930", side="BUY", quantity="10",
            order_type="LIMIT", limit_price="75000", requested_by="market_agent",
        )
        store.approve(r["approval_id"], decided_by="operator-jcpr")
        result = handlers.execute_approved_action(approval_id=r["approval_id"], actor="operator-jcpr")
        assert result["success"] is True
        assert result["state"] == "EXECUTED"
        assert result["broker_order_id"] == "B-12345"
        assert len(broker.place_order_calls) == 1

    def test_cancel_order_dispatches_to_broker(self, handlers, store, broker):
        r = handlers.request_cancel_order(
            broker_order_id="B-99999", symbol="005930", requested_by="risk_agent",
        )
        store.approve(r["approval_id"], decided_by="operator-jcpr")
        result = handlers.execute_approved_action(approval_id=r["approval_id"], actor="operator-jcpr")
        assert result["state"] == "EXECUTED"
        assert len(broker.cancel_order_calls) == 1

    def test_set_capacity_no_broker_call(self, handlers, store, broker):
        r = handlers.request_set_capacity(
            new_capacity_krw="100000000",
            rationale="quarterly capacity ladder step",
            requested_by="risk_agent",
        )
        store.approve(r["approval_id"], decided_by="operator-jcpr")
        result = handlers.execute_approved_action(approval_id=r["approval_id"], actor="operator-jcpr")
        assert result["state"] == "EXECUTED"
        assert len(broker.place_order_calls) == 0

    def test_kill_switch_no_broker_call(self, handlers, store, broker):
        r = handlers.request_kill_switch(reason="emergency stop", requested_by="risk_agent")
        # kill_switch 승인은 operator가 수행 (자가 승인 정책은 Phase 1 store 구현에 따름)
        store.approve(r["approval_id"], decided_by="operator-jcpr")
        result = handlers.execute_approved_action(approval_id=r["approval_id"], actor="operator-jcpr")
        assert result["state"] == "EXECUTED"
        assert len(broker.place_order_calls) == 0

    def test_execute_unapproved_raises(self, handlers):
        r = handlers.request_submit_order(
            symbol="005930", side="BUY", quantity="10", order_type="MARKET", requested_by="market_agent"
        )
        with pytest.raises(WriteHandlerError, match="expected APPROVED"):
            handlers.execute_approved_action(approval_id=r["approval_id"], actor="operator-jcpr")

    def test_idempotent_on_terminal(self, handlers, store, broker):
        r = handlers.request_submit_order(
            symbol="005930", side="BUY", quantity="10", order_type="MARKET", requested_by="market_agent"
        )
        store.approve(r["approval_id"], decided_by="operator-jcpr")
        handlers.execute_approved_action(approval_id=r["approval_id"], actor="operator-jcpr")
        second = handlers.execute_approved_action(approval_id=r["approval_id"], actor="operator-jcpr")
        assert second["state"] == "EXECUTED"
        assert len(broker.place_order_calls) == 1

# ---------------------------------------------------------------------------
# Internal CLI handlers
# ---------------------------------------------------------------------------
class TestInternalHandlers:
    def test_approve_action_happy_path(self, handlers, store):
        r = handlers.request_submit_order(
            symbol="005930", side="BUY", quantity="10", order_type="MARKET", requested_by="market_agent"
        )
        result = handlers.approve_action(approval_id=r["approval_id"], decided_by="operator-jcpr")
        assert result["state"] == "APPROVED"

    def test_self_approval_blocked(self, handlers):
        r = handlers.request_submit_order(
            symbol="005930", side="BUY", quantity="10", order_type="MARKET", requested_by="market_agent"
        )
        with pytest.raises(WriteHandlerError, match="self-approval"):
            handlers.approve_action(approval_id=r["approval_id"], decided_by="market_agent")

    def test_reject_action_happy_path(self, handlers):
        r = handlers.request_submit_order(
            symbol="005930", side="BUY", quantity="10", order_type="MARKET", requested_by="market_agent"
        )
        result = handlers.reject_action(
            approval_id=r["approval_id"], decided_by="operator-jcpr", reason="market closed"
        )
        assert result["state"] == "REJECTED"

    def test_reject_requires_reason(self, handlers):
        r = handlers.request_submit_order(
            symbol="005930", side="BUY", quantity="10", order_type="MARKET", requested_by="market_agent"
        )
        with pytest.raises(WriteHandlerError, match="reason required"):
            handlers.reject_action(approval_id=r["approval_id"], decided_by="operator-jcpr", reason="")
