"""Stage 2A unit tests — ExecutionGateway with real Phase 1 ApprovalStore."""
from __future__ import annotations
from datetime import datetime, timezone
from decimal import Decimal
import pytest

from src.execution.approval_store import ApprovalState, ApprovalStore
from src.execution._action_kind import ActionKind
from src.execution.execution_gateway import (
    ExecutionGateway, GatewayError,
    InterruptedExecutionError, LiveModeBlockedError,
)
from tests._stubs import MockBroker, OrderRequest, OrderSide, OrderType


def _req(symbol="005930", side=OrderSide.BUY, quantity=Decimal("10"),
         order_type=OrderType.LIMIT, limit_price=Decimal("75000"),
         coid="jcpr-001", strategy="mom_v1"):
    return OrderRequest(
        symbol=symbol, side=side, order_type=order_type,
        quantity=quantity, limit_price_krw=limit_price,
        client_order_id=coid, strategy_id=strategy,
        approval_id="", requested_at_utc=datetime.now(timezone.utc),
    )


@pytest.fixture
def store(tmp_path):
    return ApprovalStore(db_path=tmp_path / "approvals.sqlite")

@pytest.fixture
def broker():
    return MockBroker()

@pytest.fixture
def gw(store, broker):
    return ExecutionGateway(approval_store=store, broker=broker, mode="paper")


class TestGatewayConstruction:
    def test_default_paper_mode(self, store, broker):
        g = ExecutionGateway(approval_store=store, broker=broker)
        assert g.mode == "paper" and g.allow_live is False

    def test_live_without_allow_live_blocked(self, store, broker):
        with pytest.raises(LiveModeBlockedError):
            ExecutionGateway(approval_store=store, broker=broker,
                             mode="live", allow_live=False)

    def test_live_with_allow_live_succeeds(self, store, broker):
        g = ExecutionGateway(approval_store=store, broker=broker,
                             mode="live", allow_live=True)
        assert g.mode == "live"

    def test_invalid_mode_rejected(self, store, broker):
        with pytest.raises(ValueError):
            ExecutionGateway(approval_store=store, broker=broker, mode="bad")

    def test_store_property(self, gw, store):
        assert gw.store is store


class TestProposeOrder:
    def test_returns_approval_id(self, gw):
        aid = gw.propose_order(_req(), requested_by="market_agent")
        assert aid.startswith("apv-")

    def test_creates_proposed_record(self, gw, store):
        aid = gw.propose_order(_req(), requested_by="market_agent")
        r = store.get(aid)
        assert r.state == ApprovalState.PROPOSED
        assert r.action_kind == ActionKind.SUBMIT_ORDER.value
        assert r.requested_by == "market_agent"

    def test_serializes_decimal_to_string(self, gw, store):
        aid = gw.propose_order(_req(), requested_by="market_agent")
        r = store.get(aid)
        assert r.payload["quantity"] == "10"
        assert r.payload["limit_price_krw"] == "75000"

    def test_requires_requester(self, gw):
        with pytest.raises(ValueError):
            gw.propose_order(_req(), requested_by="")


class TestExecuteApproved:
    def test_happy_path(self, gw, store, broker):
        aid = gw.propose_order(_req(), requested_by="market_agent")
        store.approve(aid, decided_by="operator-jcpr")
        result = gw.execute_approved(aid, actor="operator-jcpr")
        assert result.success is True
        assert result.state == ApprovalState.EXECUTED
        assert result.broker_order_id == "B-12345"
        assert len(broker.place_order_calls) == 1

    def test_executed_persisted(self, gw, store):
        aid = gw.propose_order(_req(), requested_by="market_agent")
        store.approve(aid, decided_by="operator-jcpr")
        gw.execute_approved(aid, actor="operator-jcpr")
        assert store.get(aid).state == ApprovalState.EXECUTED

    def test_broker_rejection(self, store):
        b = MockBroker(accepted=False, error_message="insufficient funds")
        g = ExecutionGateway(approval_store=store, broker=b)
        aid = g.propose_order(_req(), requested_by="market_agent")
        store.approve(aid, decided_by="operator-jcpr")
        r = g.execute_approved(aid, actor="operator-jcpr")
        assert r.success is False and r.state == ApprovalState.EXEC_FAILED

    def test_broker_exception(self, store):
        b = MockBroker(raise_exception=RuntimeError("KIS timeout"))
        g = ExecutionGateway(approval_store=store, broker=b)
        aid = g.propose_order(_req(), requested_by="market_agent")
        store.approve(aid, decided_by="operator-jcpr")
        with pytest.raises(GatewayError):
            g.execute_approved(aid, actor="operator-jcpr")
        assert store.get(aid).state == ApprovalState.EXEC_FAILED

    def test_idempotent_executed(self, gw, store, broker):
        aid = gw.propose_order(_req(), requested_by="market_agent")
        store.approve(aid, decided_by="operator-jcpr")
        gw.execute_approved(aid, actor="operator-jcpr")
        gw.execute_approved(aid, actor="operator-jcpr")
        assert len(broker.place_order_calls) == 1

    def test_idempotent_failed(self, store):
        b = MockBroker(accepted=False, error_message="rejected")
        g = ExecutionGateway(approval_store=store, broker=b)
        aid = g.propose_order(_req(), requested_by="market_agent")
        store.approve(aid, decided_by="operator-jcpr")
        g.execute_approved(aid, actor="operator-jcpr")
        second = g.execute_approved(aid, actor="operator-jcpr")
        assert len(b.place_order_calls) == 1
        assert second.state == ApprovalState.EXEC_FAILED

    def test_unknown_id_raises(self, gw):
        with pytest.raises(GatewayError):
            gw.execute_approved("apv-99999999-deadbeef", actor="op")

    def test_actor_required(self, gw, store):
        aid = gw.propose_order(_req(), requested_by="market_agent")
        store.approve(aid, decided_by="operator-jcpr")
        with pytest.raises(ValueError):
            gw.execute_approved(aid, actor="")

    def test_mode_mismatch_raises(self, store):
        b = MockBroker()
        paper = ExecutionGateway(approval_store=store, broker=b, mode="paper")
        aid = paper.propose_order(_req(), requested_by="market_agent")
        store.approve(aid, decided_by="operator-jcpr")
        live = ExecutionGateway(approval_store=store, broker=b,
                                mode="live", allow_live=True)
        with pytest.raises(GatewayError, match="mode"):
            live.execute_approved(aid, actor="operator-jcpr")


class TestInterruptHandling:
    def test_interrupt_at_propose(self, store, broker):
        flag = {"v": False}
        g = ExecutionGateway(approval_store=store, broker=broker,
                              interrupt_check=lambda: flag["v"])
        flag["v"] = True
        with pytest.raises(InterruptedExecutionError):
            g.propose_order(_req(), requested_by="market_agent")

    def test_interrupt_before_mark_executing_leaves_approved(self, store, broker):
        flag = {"v": False}
        g = ExecutionGateway(approval_store=store, broker=broker,
                              interrupt_check=lambda: flag["v"])
        aid = g.propose_order(_req(), requested_by="market_agent")
        store.approve(aid, decided_by="operator-jcpr")
        flag["v"] = True
        with pytest.raises(InterruptedExecutionError):
            g.execute_approved(aid, actor="operator-jcpr")
        assert store.get(aid).state == ApprovalState.APPROVED

    def test_interrupt_during_broker_call_marks_exec_failed(self, store):
        b = MockBroker()
        g = ExecutionGateway(approval_store=store, broker=b,
                              interrupt_check=lambda: False)
        aid = g.propose_order(_req(), requested_by="market_agent")
        store.approve(aid, decided_by="operator-jcpr")
        count = {"n": 0}
        def staged():
            count["n"] += 1
            return count["n"] >= 2
        g._interrupt_check = staged
        with pytest.raises(InterruptedExecutionError):
            g.execute_approved(aid, actor="operator-jcpr")
        r = store.get(aid)
        assert r.state == ApprovalState.EXEC_FAILED
        err = (r.decision_reason or "") + str(r.execution_result or {})
        assert "interrupted" in err.lower()
        assert len(b.place_order_calls) == 0


class TestCancelProposed:
    def test_cancel_pending_succeeds(self, gw, store):
        aid = gw.propose_order(_req(), requested_by="market_agent")
        gw.cancel_proposed(aid, actor="market_agent", reason="changed mind")
        assert store.get(aid).state == ApprovalState.CANCELLED

    def test_cancel_after_approve_raises(self, gw, store):
        aid = gw.propose_order(_req(), requested_by="market_agent")
        store.approve(aid, decided_by="operator-jcpr")
        with pytest.raises(Exception):
            gw.cancel_proposed(aid, actor="market_agent")
