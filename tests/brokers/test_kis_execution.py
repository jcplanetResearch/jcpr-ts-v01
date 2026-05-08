"""Tests for brokers/kis_execution.py — write operations."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from src.brokers._secrets import KISSecrets, SecretValue
from src.brokers.base import (
    BrokerMode,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
)
from src.brokers.kis_execution import (
    KISExecutionAdapter,
    ORDER_CANCEL_PATH,
    ORDER_PLACE_PATH,
)
# Reuse MockOpener from kis_adapter tests
from tests.brokers.test_kis_adapter import MockOpener


@pytest.fixture
def paper_secrets() -> KISSecrets:
    return KISSecrets(
        appkey=SecretValue("PSED" + "A" * 32),
        appsecret=SecretValue("Z" * 180),
        account_number="12345678",
        account_product="01",
        mode="paper",
    )


@pytest.fixture
def fixed_now() -> datetime:
    return datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def adapter(paper_secrets, fixed_now, tmp_path):
    opener = MockOpener()
    a = KISExecutionAdapter(
        secrets=paper_secrets,
        mode=BrokerMode.PAPER,
        token_cache_path=tmp_path / "tok.json",
        _now_fn=lambda: fixed_now,
        _opener=opener,
    )
    return a, opener


@pytest.fixture
def buy_request(fixed_now) -> OrderRequest:
    return OrderRequest(
        symbol="005930",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("10"),
        limit_price_krw=Decimal("70000"),
        client_order_id="strat-001-x",
        strategy_id="strat-001",
        approval_id="ap-test-1",
        requested_at_utc=fixed_now,
    )


# =============================================================================
# place_order
# =============================================================================

class TestPlaceOrder:
    def test_successful_buy(self, adapter, buy_request):
        a, opener = adapter
        opener.queue(200, {"access_token": "tok", "expires_in": 86400})
        opener.queue(200, {
            "rt_cd": "0",
            "msg_cd": "OK",
            "msg1": "주문 체결",
            "output": {"ODNO": "0000123456", "ORD_TMD": "093000"},
        })
        response = a.place_order(buy_request)
        assert response.success is True
        assert response.broker_order_id == "0000123456"
        assert response.client_order_id == "strat-001-x"
        assert response.status == OrderStatus.PENDING

        # Verify URL hit
        order_call = opener.calls[1]  # [0]=token, [1]=order
        assert ORDER_PLACE_PATH in order_call["url"]

    def test_uses_paper_buy_tr_id(self, adapter, buy_request):
        a, opener = adapter
        opener.queue(200, {"access_token": "tok", "expires_in": 86400})
        opener.queue(200, {"rt_cd": "0", "output": {"ODNO": "1"}})
        a.place_order(buy_request)
        # Header 'Tr_id' set to paper buy
        assert opener.calls[1]["headers"]["Tr_id"] == "VTTC0802U"

    def test_sell_uses_sell_tr_id(self, adapter, buy_request, fixed_now):
        sell = OrderRequest(
            symbol="005930",
            side=OrderSide.SELL,
            order_type=OrderType.LIMIT,
            quantity=Decimal("5"),
            limit_price_krw=Decimal("75000"),
            client_order_id="x",
            strategy_id="s",
            approval_id="ap-2",
            requested_at_utc=fixed_now,
        )
        a, opener = adapter
        opener.queue(200, {"access_token": "tok", "expires_in": 86400})
        opener.queue(200, {"rt_cd": "0", "output": {"ODNO": "1"}})
        a.place_order(sell)
        assert opener.calls[1]["headers"]["Tr_id"] == "VTTC0801U"

    def test_market_order_zero_price(self, adapter, fixed_now):
        market = OrderRequest(
            symbol="005930",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("3"),
            limit_price_krw=None,
            client_order_id="m1",
            strategy_id="s",
            approval_id="ap-m",
            requested_at_utc=fixed_now,
        )
        a, opener = adapter
        opener.queue(200, {"access_token": "tok", "expires_in": 86400})
        opener.queue(200, {"rt_cd": "0", "output": {"ODNO": "M1"}})
        a.place_order(market)
        body = json.loads(opener.calls[1]["body"])
        assert body["ORD_DVSN"] == "01"  # market
        assert body["ORD_UNPR"] == "0"

    def test_kis_error_returns_failure(self, adapter, buy_request):
        a, opener = adapter
        opener.queue(200, {"access_token": "tok", "expires_in": 86400})
        opener.queue(200, {
            "rt_cd": "1",
            "msg_cd": "EGW00121",
            "msg1": "잔고가 부족합니다",
        })
        response = a.place_order(buy_request)
        assert response.success is False
        assert response.status == OrderStatus.REJECTED
        assert response.error_code == "EGW00121"
        assert "잔고" in response.error_message

    def test_http_error_returns_failure(self, adapter, buy_request):
        a, opener = adapter
        opener.queue(200, {"access_token": "tok", "expires_in": 86400})
        opener.queue(500, {"msg1": "server error"})
        response = a.place_order(buy_request)
        assert response.success is False
        assert response.broker_order_id is None

    def test_interrupted_blocks(self, adapter, buy_request):
        a, opener = adapter
        a.signal_interrupt()
        with pytest.raises(Exception, match="interrupted"):
            a.place_order(buy_request)

    def test_rejects_non_OrderRequest(self, adapter):
        a, _ = adapter
        with pytest.raises(TypeError, match="OrderRequest"):
            a.place_order("not an OrderRequest")  # type: ignore

    def test_quantity_serialized_as_int(self, adapter, buy_request):
        a, opener = adapter
        opener.queue(200, {"access_token": "tok", "expires_in": 86400})
        opener.queue(200, {"rt_cd": "0", "output": {"ODNO": "X"}})
        a.place_order(buy_request)
        body = json.loads(opener.calls[1]["body"])
        # KIS expects ORD_QTY as integer string
        assert body["ORD_QTY"] == "10"
        assert body["ORD_UNPR"] == "70000"
        assert body["PDNO"] == "005930"


# =============================================================================
# cancel_order
# =============================================================================

class TestCancelOrder:
    def test_successful_cancel(self, adapter):
        a, opener = adapter
        opener.queue(200, {"access_token": "tok", "expires_in": 86400})
        opener.queue(200, {
            "rt_cd": "0",
            "msg1": "취소 완료",
            "output": {"ODNO": "0000123456"},
        })
        response = a.cancel_order(
            broker_order_id="0000123456",
            approval_id="ap-cancel-1",
        )
        assert response.success is True
        assert response.broker_order_id == "0000123456"
        assert response.status == OrderStatus.CANCELLED

    def test_uses_paper_cancel_tr_id(self, adapter):
        a, opener = adapter
        opener.queue(200, {"access_token": "tok", "expires_in": 86400})
        opener.queue(200, {"rt_cd": "0"})
        a.cancel_order(broker_order_id="x", approval_id="ap-x")
        assert opener.calls[1]["headers"]["Tr_id"] == "VTTC0803U"

    def test_kis_error(self, adapter):
        a, opener = adapter
        opener.queue(200, {"access_token": "tok", "expires_in": 86400})
        opener.queue(200, {
            "rt_cd": "1",
            "msg_cd": "EGW00999",
            "msg1": "이미 체결된 주문",
        })
        response = a.cancel_order(
            broker_order_id="x",
            approval_id="ap-x",
        )
        assert response.success is False
        assert "체결" in response.error_message

    def test_rejects_empty_order_id(self, adapter):
        a, _ = adapter
        with pytest.raises(ValueError, match="broker_order_id"):
            a.cancel_order(broker_order_id="", approval_id="ap-x")

    def test_rejects_empty_approval_id(self, adapter):
        a, _ = adapter
        with pytest.raises(ValueError, match="approval_id"):
            a.cancel_order(broker_order_id="x", approval_id="")

    def test_interrupted_blocks(self, adapter):
        a, _ = adapter
        a.signal_interrupt()
        with pytest.raises(Exception, match="interrupted"):
            a.cancel_order(broker_order_id="x", approval_id="ap-x")
