"""Tests for brokers/base.py — frozen dataclasses + interfaces."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

# Path setup
_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from src.brokers.base import (
    AccountSummary,
    BrokerAdapter,
    BrokerExecutionInterface,
    BrokerMode,
    ConnectionCheck,
    Order,
    OrderRequest,
    OrderResponse,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
)


@pytest.fixture
def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


# =============================================================================
# Enums
# =============================================================================

class TestEnums:
    def test_broker_mode_values(self):
        assert BrokerMode.PAPER.value == "paper"
        assert BrokerMode.PROD.value == "prod"

    def test_order_side(self):
        assert OrderSide.BUY.value == "buy"
        assert OrderSide.SELL.value == "sell"

    def test_order_type(self):
        assert OrderType.MARKET.value == "market"
        assert OrderType.LIMIT.value == "limit"

    def test_order_status_has_required_states(self):
        required = {"pending", "partially_filled", "filled", "cancelled", "rejected"}
        actual = {s.value for s in OrderStatus}
        assert required == actual


# =============================================================================
# AccountSummary
# =============================================================================

class TestAccountSummary:
    def test_accepts_valid(self, utc_now):
        a = AccountSummary(
            account_id_masked="1234***",
            cash_balance_krw=Decimal("1000000"),
            total_equity_krw=Decimal("1500000"),
            buying_power_krw=Decimal("800000"),
            mode=BrokerMode.PAPER,
            fetched_at_utc=utc_now,
        )
        assert a.cash_balance_krw == Decimal("1000000")

    def test_rejects_non_decimal(self, utc_now):
        with pytest.raises(TypeError, match="must be Decimal"):
            AccountSummary(
                account_id_masked="x",
                cash_balance_krw=1000000,  # int
                total_equity_krw=Decimal("0"),
                buying_power_krw=Decimal("0"),
                mode=BrokerMode.PAPER,
                fetched_at_utc=utc_now,
            )

    def test_rejects_naive_datetime(self):
        with pytest.raises(ValueError, match="tz-aware"):
            AccountSummary(
                account_id_masked="x",
                cash_balance_krw=Decimal("0"),
                total_equity_krw=Decimal("0"),
                buying_power_krw=Decimal("0"),
                mode=BrokerMode.PAPER,
                fetched_at_utc=datetime(2026, 5, 7),
            )

    def test_is_frozen(self, utc_now):
        a = AccountSummary(
            account_id_masked="x",
            cash_balance_krw=Decimal("0"),
            total_equity_krw=Decimal("0"),
            buying_power_krw=Decimal("0"),
            mode=BrokerMode.PAPER,
            fetched_at_utc=utc_now,
        )
        with pytest.raises((AttributeError, Exception)):
            a.cash_balance_krw = Decimal("999")  # type: ignore


# =============================================================================
# Position
# =============================================================================

class TestPosition:
    def test_accepts_valid(self):
        p = Position(
            symbol="005930",
            quantity=Decimal("10"),
            avg_cost_krw=Decimal("70000"),
            current_price_krw=Decimal("71000"),
            market_value_krw=Decimal("710000"),
            unrealized_pnl_krw=Decimal("10000"),
        )
        assert p.symbol == "005930"

    def test_rejects_empty_symbol(self):
        with pytest.raises(ValueError, match="non-empty"):
            Position(
                symbol="",
                quantity=Decimal("0"),
                avg_cost_krw=Decimal("0"),
                current_price_krw=Decimal("0"),
                market_value_krw=Decimal("0"),
                unrealized_pnl_krw=Decimal("0"),
            )

    def test_rejects_non_decimal_quantity(self):
        with pytest.raises(TypeError, match="must be Decimal"):
            Position(
                symbol="x",
                quantity=10,  # int
                avg_cost_krw=Decimal("0"),
                current_price_krw=Decimal("0"),
                market_value_krw=Decimal("0"),
                unrealized_pnl_krw=Decimal("0"),
            )


# =============================================================================
# Order
# =============================================================================

class TestOrder:
    def test_accepts_valid(self, utc_now):
        o = Order(
            order_id="ord-1",
            symbol="005930",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Decimal("10"),
            filled_quantity=Decimal("0"),
            limit_price_krw=Decimal("70000"),
            avg_fill_price_krw=None,
            status=OrderStatus.PENDING,
            placed_at_utc=utc_now,
            last_updated_utc=utc_now,
        )
        assert o.status == OrderStatus.PENDING

    def test_rejects_empty_order_id(self, utc_now):
        with pytest.raises(ValueError, match="non-empty"):
            Order(
                order_id="",
                symbol="x",
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                quantity=Decimal("1"),
                filled_quantity=Decimal("0"),
                limit_price_krw=None,
                avg_fill_price_krw=None,
                status=OrderStatus.PENDING,
                placed_at_utc=utc_now,
                last_updated_utc=utc_now,
            )


# =============================================================================
# OrderRequest — Task 40 contract
# =============================================================================

class TestOrderRequest:
    def test_limit_order_requires_price(self, utc_now):
        with pytest.raises(ValueError, match="limit_price_krw required"):
            OrderRequest(
                symbol="x",
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                quantity=Decimal("1"),
                limit_price_krw=None,
                client_order_id="oid-1",
                strategy_id="s",
                approval_id="ap-1",
                requested_at_utc=utc_now,
            )

    def test_market_order_no_price_ok(self, utc_now):
        r = OrderRequest(
            symbol="x",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("1"),
            limit_price_krw=None,
            client_order_id="oid-1",
            strategy_id="s",
            approval_id="ap-1",
            requested_at_utc=utc_now,
        )
        assert r.limit_price_krw is None

    def test_rejects_zero_quantity(self, utc_now):
        with pytest.raises(ValueError, match="quantity must be positive"):
            OrderRequest(
                symbol="x",
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quantity=Decimal("0"),
                limit_price_krw=None,
                client_order_id="oid-1",
                strategy_id="s",
                approval_id="ap-1",
                requested_at_utc=utc_now,
            )

    def test_rejects_negative_quantity(self, utc_now):
        with pytest.raises(ValueError, match="quantity must be positive"):
            OrderRequest(
                symbol="x",
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quantity=Decimal("-1"),
                limit_price_krw=None,
                client_order_id="oid-1",
                strategy_id="s",
                approval_id="ap-1",
                requested_at_utc=utc_now,
            )

    def test_rejects_empty_approval_id(self, utc_now):
        with pytest.raises(ValueError, match="approval_id required"):
            OrderRequest(
                symbol="x",
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quantity=Decimal("1"),
                limit_price_krw=None,
                client_order_id="oid-1",
                strategy_id="s",
                approval_id="",
                requested_at_utc=utc_now,
            )

    def test_rejects_too_long_client_order_id(self, utc_now):
        with pytest.raises(ValueError, match="client_order_id"):
            OrderRequest(
                symbol="x",
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quantity=Decimal("1"),
                limit_price_krw=None,
                client_order_id="x" * 100,
                strategy_id="s",
                approval_id="ap-1",
                requested_at_utc=utc_now,
            )


# =============================================================================
# Abstract interfaces
# =============================================================================

class TestAbstractInterfaces:
    def test_broker_adapter_cannot_be_instantiated(self):
        with pytest.raises(TypeError):
            BrokerAdapter()  # type: ignore

    def test_broker_execution_interface_cannot_be_instantiated(self):
        with pytest.raises(TypeError):
            BrokerExecutionInterface()  # type: ignore
