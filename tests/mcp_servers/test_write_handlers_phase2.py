"""Stage 2A unit tests — WriteHandlers with real Phase 1 ApprovalStore."""
from __future__ import annotations
from datetime import datetime, timezone
from decimal import Decimal
import pytest

from src.execution.approval_store import ApprovalState, ApprovalStore
from src.execution._action_kind import ActionKind
from src.execution.execution_gateway import ExecutionGateway
from src.mcp_servers._write_handlers import WriteHandlerError, build_handlers
from tests._stubs import MockBroker


@pytest.fixture
def store(tmp_path):
    return ApprovalStore(db_path=tmp_path / "approvals.sqlite")

@pytest.fixture
def broker():
    return MockBroker()

@pytest.fixture
def gw(store, broker):
    return ExecutionGateway(approval_store=store, broker=broker, mode="paper")

@pytest.fixture
def h(store, gw):
    return build_handlers(store=store, gateway=gw, operator_id="operator-jcpr")


class TestBuildHandlers:
    def test_requires_store(self, gw):
        with pytest.raises(ValueError):
            build_handlers(store=None, gateway=gw, operator_id="op")

    def test_requires_gateway(self, store):
        with pytest.raises(ValueError):
            build_handlers(store=store, gateway=None, operator_id="op")

    def test_requires_operator_id(self, store, gw):
        with pytest.raises(ValueError):
            build_handlers(store=store, gateway=gw, operator_id="")

    def test_rejects_mismatched_store(self, store, broker, tmp_path):
        other = ApprovalStore(db_path=tmp_path / "other.sqlite")
        gw2 = ExecutionGateway(approval_store=other, broker=broker)
        with pytest.raises(ValueError, match="unified-store"):
            build_handlers(store=store, gateway=gw2, operator_id="op")


class TestRequestSubmitOrder:
    def test_happy_path_limit_order(self, h, store):
        r = h.request_submit_order(
            symbol="005930", side="BUY", quantity="10", order_type="LIMIT",
            limit_price="75000", requested_by="market_agent")
        assert r["state"] == ApprovalState.PROPOSED.value
        assert store.get(r["approval_id"]).payload["symbol"] == "005930"

    def test_market_order_no_limit_price(self, h):
        r = h.request_submit_order(
            symbol="005930", side="SELL", quantity="5",
            order_type="MARKET", requested_by="market_agent")
        assert r["state"] == ApprovalState.PROPOSED.value

    def test_limit_order_requires_limit_price(self, h):
        with pytest.raises(WriteHandlerError, match="LIMIT order requires"):
            h.request_submit_order(
                symbol="005930", side="BUY", quantity="10",
                order_type="LIMIT", requested_by="market_agent")

    def test_invalid_side_rejected(self, h):
        with pytest.raises(WriteHandlerError, match="side must be"):
            h.request_submit_order(
                symbol="005930", side="HOLD", quantity="10",
                order_type="MARKET", requested_by="market_agent")

    def test_zero_quantity_rejected(self, h):
        with pytest.raises(WriteHandlerError, match="quantity must be"):
            h.request_submit_order(
                symbol="005930", side="BUY", quantity="0",
                order_type="MARKET", requested_by="market_agent")

    def test_invalid_decimal_rejected(self, h):
        with pytest.raises(WriteHandlerError, match="invalid quantity"):
            h.request_submit_order(
                symbol="005930", side="BUY", quantity="abc",
                order_type="MARKET", requested_by="market_agent")

    def test_generic_actor_rejected(self, h):
        with pytest.raises(WriteHandlerError, match="too generic"):
            h.request_submit_order(
                symbol="005930", side="BUY", quantity="10",
                order_type="MARKET", requested_by="agent")

    def test_empty_actor_rejected(self, h):
        with pytest.raises(WriteHandlerError, match="required"):
            h.request_submit_order(
                symbol="005930", side="BUY", quantity="10",
                order_type="MARKET", requested_by="")

    def test_auto_generated_client_order_id(self, h, store):
        r = h.request_submit_order(
            symbol="005930", side="BUY", quantity="10",
            order_type="MARKET", requested_by="market_agent")
        assert store.get(r["approval_id"]).payload["client_order_id"].startswith("jcpr-")


class TestOtherRequestHandlers:
    def test_cancel_order_creates_proposal(self, h, store):
        r = h.request_cancel_order(
            broker_order_id="B-12345", symbol="005930", requested_by="risk_agent")
        assert store.get(r["approval_id"]).action_kind == ActionKind.CANCEL_ORDER.value

    def test_cancel_order_requires_broker_id(self, h):
        with pytest.raises(WriteHandlerError):
            h.request_cancel_order(broker_order_id="", symbol="005930",
                                    requested_by="risk_agent")

    def test_set_capacity_happy_path(self, h, store):
        r = h.request_set_capacity(
            new_capacity_krw="50000000",
            rationale="weekly capacity expansion ladder",
            requested_by="risk_agent")
        assert store.get(r["approval_id"]).action_kind == ActionKind.SET_CAPACITY.value

    def test_set_capacity_negative_rejected(self, h):
        with pytest.raises(WriteHandlerError, match="non-negative"):
            h.request_set_capacity(new_capacity_krw="-1000",
                                    rationale="test rationale here",
                                    requested_by="risk_agent")

    def test_set_capacity_short_rationale_rejected(self, h):
        with pytest.raises(WriteHandlerError, match="at least 10"):
            h.request_set_capacity(new_capacity_krw="10000", rationale="x",
                                    requested_by="risk_agent")

    def test_kill_switch_happy_path(self, h, store):
        r = h.request_kill_switch(reason="anomaly detected", requested_by="risk_agent")
        assert store.get(r["approval_id"]).action_kind == ActionKind.KILL_SWITCH.value

    def test_kill_switch_short_reason_rejected(self, h):
        with pytest.raises(WriteHandlerError, match="at least 5"):
            h.request_kill_switch(reason="ok", requested_by="risk_agent")


class TestListAndGet:
    def test_list_empty(self, h):
        assert h.list_pending_approvals()["count"] == 0

    def test_list_returns_pending(self, h):
        h.request_submit_order(symbol="005930", side="BUY", quantity="10",
                                order_type="MARKET", requested_by="market_agent")
        h.request_kill_switch(reason="manual test", requested_by="risk_agent")
        assert h.list_pending_approvals()["count"] == 2

    def test_list_filtered_by_requester(self, h):
        h.request_submit_order(symbol="005930", side="BUY", quantity="10",
                                order_type="MARKET", requested_by="market_agent")
        h.request_kill_switch(reason="manual test", requested_by="risk_agent")
        assert h.list_pending_approvals(requested_by="market_agent")["count"] == 1

    def test_get_detail_returns_payload(self, h):
        r = h.request_submit_order(symbol="005930", side="BUY", quantity="10",
                                    order_type="MARKET", requested_by="market_agent")
        d = h.get_approval_detail(approval_id=r["approval_id"])
        assert d["action_payload"]["symbol"] == "005930"

    def test_get_detail_unknown_raises(self, h):
        with pytest.raises(WriteHandlerError, match="not found"):
            h.get_approval_detail(approval_id="apv-99999999-deadbeef")

    def test_cancel_proposed_by_requester(self, h, store):
        r = h.request_submit_order(symbol="005930", side="BUY", quantity="10",
                                    order_type="MARKET", requested_by="market_agent")
        result = h.cancel_proposed_action(approval_id=r["approval_id"],
                                           actor="market_agent")
        assert result["state"] == ApprovalState.CANCELLED.value

    def test_cancel_by_third_party_blocked(self, h):
        r = h.request_submit_order(symbol="005930", side="BUY", quantity="10",
                                    order_type="MARKET", requested_by="market_agent")
        with pytest.raises(WriteHandlerError, match="requester or the operator"):
            h.cancel_proposed_action(approval_id=r["approval_id"], actor="risk_agent")

    def test_cancel_by_operator_allowed(self, h, store):
        r = h.request_submit_order(symbol="005930", side="BUY", quantity="10",
                                    order_type="MARKET", requested_by="market_agent")
        result = h.cancel_proposed_action(approval_id=r["approval_id"],
                                           actor="operator-jcpr")
        assert result["state"] == ApprovalState.CANCELLED.value


class TestExecuteApprovedAction:
    def test_submit_order_dispatches_to_gateway(self, h, store, broker):
        r = h.request_submit_order(symbol="005930", side="BUY", quantity="10",
                                    order_type="LIMIT", limit_price="75000",
                                    requested_by="market_agent")
        store.approve(r["approval_id"], decided_by="operator-jcpr")
        result = h.execute_approved_action(approval_id=r["approval_id"],
                                            actor="operator-jcpr")
        assert result["success"] is True
        assert result["state"] == ApprovalState.EXECUTED.value
        assert len(broker.place_order_calls) == 1

    def test_cancel_order_dispatches_to_broker(self, h, store, broker):
        r = h.request_cancel_order(broker_order_id="B-99999", symbol="005930",
                                    requested_by="risk_agent")
        store.approve(r["approval_id"], decided_by="operator-jcpr")
        result = h.execute_approved_action(approval_id=r["approval_id"],
                                            actor="operator-jcpr")
        assert result["state"] == ApprovalState.EXECUTED.value
        assert len(broker.cancel_order_calls) == 1

    def test_set_capacity_no_broker_call(self, h, store, broker):
        r = h.request_set_capacity(new_capacity_krw="100000000",
                                    rationale="quarterly capacity ladder",
                                    requested_by="risk_agent")
        store.approve(r["approval_id"], decided_by="operator-jcpr")
        result = h.execute_approved_action(approval_id=r["approval_id"],
                                            actor="operator-jcpr")
        assert result["state"] == ApprovalState.EXECUTED.value
        assert len(broker.place_order_calls) == 0

    def test_kill_switch_no_broker_call(self, h, store, broker):
        r = h.request_kill_switch(reason="emergency stop", requested_by="risk_agent")
        store.approve(r["approval_id"], decided_by="operator-jcpr")
        result = h.execute_approved_action(approval_id=r["approval_id"],
                                            actor="operator-jcpr")
        assert result["state"] == ApprovalState.EXECUTED.value
        assert len(broker.place_order_calls) == 0

    def test_execute_unapproved_raises(self, h):
        r = h.request_submit_order(symbol="005930", side="BUY", quantity="10",
                                    order_type="MARKET", requested_by="market_agent")
        with pytest.raises(WriteHandlerError, match="expected APPROVED"):
            h.execute_approved_action(approval_id=r["approval_id"],
                                       actor="operator-jcpr")

    def test_idempotent_on_terminal(self, h, store, broker):
        r = h.request_submit_order(symbol="005930", side="BUY", quantity="10",
                                    order_type="MARKET", requested_by="market_agent")
        store.approve(r["approval_id"], decided_by="operator-jcpr")
        h.execute_approved_action(approval_id=r["approval_id"], actor="operator-jcpr")
        second = h.execute_approved_action(approval_id=r["approval_id"],
                                            actor="operator-jcpr")
        assert second["state"] == ApprovalState.EXECUTED.value
        assert len(broker.place_order_calls) == 1


class TestInternalHandlers:
    def test_approve_action_happy_path(self, h, store):
        r = h.request_submit_order(symbol="005930", side="BUY", quantity="10",
                                    order_type="MARKET", requested_by="market_agent")
        result = h.approve_action(approval_id=r["approval_id"],
                                   decided_by="operator-jcpr")
        assert result["state"] == ApprovalState.APPROVED.value

    def test_self_approval_blocked(self, h):
        r = h.request_submit_order(symbol="005930", side="BUY", quantity="10",
                                    order_type="MARKET", requested_by="market_agent")
        with pytest.raises(WriteHandlerError, match="self-approval"):
            h.approve_action(approval_id=r["approval_id"], decided_by="market_agent")

    def test_reject_action_happy_path(self, h):
        r = h.request_submit_order(symbol="005930", side="BUY", quantity="10",
                                    order_type="MARKET", requested_by="market_agent")
        result = h.reject_action(approval_id=r["approval_id"],
                                  decided_by="operator-jcpr", reason="market closed")
        assert result["state"] == ApprovalState.REJECTED.value

    def test_reject_requires_reason(self, h):
        r = h.request_submit_order(symbol="005930", side="BUY", quantity="10",
                                    order_type="MARKET", requested_by="market_agent")
        with pytest.raises(WriteHandlerError, match="reason required"):
            h.reject_action(approval_id=r["approval_id"],
                             decided_by="operator-jcpr", reason="")
