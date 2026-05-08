"""Tests for brokers/kis_adapter.py using mocked HTTP opener."""
from __future__ import annotations

import io
import json
import os
import ssl
import sys
import threading
import urllib.error
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from src.brokers._secrets import KISSecrets, SecretValue
from src.brokers.base import (
    BrokerMode,
    OrderSide,
    OrderStatus,
    OrderType,
)
from src.brokers.kis_adapter import (
    KIS_PAPER_BASE_URL,
    KIS_PROD_BASE_URL,
    LIVE_MODE_ENV_VAR,
    KISAdapterError,
    KISBrokerAdapter,
    _build_secure_tls_context,
    _parse_kis_datetime,
    _to_decimal,
)


# =============================================================================
# Test helpers
# =============================================================================

class MockResponse:
    """Mock urllib.request response."""

    def __init__(self, body: dict, status: int = 200) -> None:
        self._body = json.dumps(body).encode("utf-8")
        self._status = status

    def getcode(self) -> int:
        return self._status

    def read(self) -> bytes:
        return self._body


class MockOpener:
    """Replaces urllib.request.urlopen for tests."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.responses: list[tuple[int, dict]] = []
        self.raise_for_call: list[Exception | None] = []

    def queue(self, status: int, body: dict) -> None:
        self.responses.append((status, body))
        self.raise_for_call.append(None)

    def queue_error(self, exc: Exception) -> None:
        self.responses.append((0, {}))
        self.raise_for_call.append(exc)

    def __call__(self, request, timeout: int):
        # Record the call
        self.calls.append({
            "url": request.full_url,
            "method": request.get_method(),
            "headers": dict(request.headers),
            "body": request.data,
        })
        if not self.responses:
            raise AssertionError("MockOpener: no response queued")
        status, body = self.responses.pop(0)
        exc = self.raise_for_call.pop(0)
        if exc is not None:
            raise exc
        return MockResponse(body, status)


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
def prod_secrets() -> KISSecrets:
    return KISSecrets(
        appkey=SecretValue("PROD" + "B" * 32),
        appsecret=SecretValue("Y" * 180),
        account_number="87654321",
        account_product="01",
        mode="prod",
    )


@pytest.fixture
def fixed_now() -> datetime:
    return datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def adapter_factory(paper_secrets, fixed_now, tmp_path):
    """Builds KIS adapter with injected mock opener and fixed clock."""
    def _build(mode: BrokerMode = BrokerMode.PAPER, opener: MockOpener | None = None,
               secrets: KISSecrets | None = None):
        opener = opener or MockOpener()
        token_path = tmp_path / f".kis_token_{mode.value}.json"
        secrets = secrets or paper_secrets
        adapter = KISBrokerAdapter(
            secrets=secrets,
            mode=mode,
            token_cache_path=token_path,
            _now_fn=lambda: fixed_now,
            _opener=opener,
        )
        return adapter, opener
    return _build


# =============================================================================
# Helpers tests
# =============================================================================

class TestHelpers:
    def test_to_decimal_handles_comma(self):
        assert _to_decimal("1,000,000") == Decimal("1000000")

    def test_to_decimal_handles_empty(self):
        assert _to_decimal("") == Decimal("0")
        assert _to_decimal(None) == Decimal("0")

    def test_to_decimal_handles_garbage(self):
        assert _to_decimal("xyz") == Decimal("0")

    def test_parse_kis_datetime(self):
        result = _parse_kis_datetime("20260507", "093000")
        assert result.tzinfo is not None
        assert result.tzinfo.utcoffset(result) == timedelta(0)  # UTC
        # 09:30 KST = 00:30 UTC
        assert result.hour == 0
        assert result.minute == 30


class TestTLSContext:
    def test_tls_context_minimum_1_2(self):
        ctx = _build_secure_tls_context()
        assert ctx.minimum_version >= ssl.TLSVersion.TLSv1_2
        assert ctx.verify_mode == ssl.CERT_REQUIRED
        assert ctx.check_hostname is True


# =============================================================================
# Adapter construction
# =============================================================================

class TestAdapterConstruction:
    def test_paper_constructs(self, adapter_factory):
        adapter, _ = adapter_factory()
        assert adapter.mode == BrokerMode.PAPER
        assert adapter.adapter_name == "kis"
        assert adapter.base_url == KIS_PAPER_BASE_URL

    def test_prod_requires_env_var(self, prod_secrets, fixed_now, tmp_path, monkeypatch):
        monkeypatch.delenv(LIVE_MODE_ENV_VAR, raising=False)
        with pytest.raises(KISAdapterError, match="JCPR_ALLOW_LIVE"):
            KISBrokerAdapter(
                secrets=prod_secrets,
                mode=BrokerMode.PROD,
                token_cache_path=tmp_path / "tok.json",
                _now_fn=lambda: fixed_now,
                _opener=MockOpener(),
            )

    def test_prod_with_env_var_constructs(self, prod_secrets, fixed_now,
                                          tmp_path, monkeypatch):
        monkeypatch.setenv(LIVE_MODE_ENV_VAR, "1")
        adapter = KISBrokerAdapter(
            secrets=prod_secrets,
            mode=BrokerMode.PROD,
            token_cache_path=tmp_path / "tok.json",
            _now_fn=lambda: fixed_now,
            _opener=MockOpener(),
        )
        assert adapter.mode == BrokerMode.PROD
        assert adapter.base_url == KIS_PROD_BASE_URL

    def test_secrets_mode_mismatch_rejected(self, paper_secrets, fixed_now,
                                            tmp_path, monkeypatch):
        monkeypatch.setenv(LIVE_MODE_ENV_VAR, "1")
        with pytest.raises(KISAdapterError, match="secrets.mode"):
            KISBrokerAdapter(
                secrets=paper_secrets,  # paper
                mode=BrokerMode.PROD,    # prod
                token_cache_path=tmp_path / "tok.json",
                _now_fn=lambda: fixed_now,
            )

    def test_repr_does_not_leak_secrets(self, adapter_factory):
        adapter, _ = adapter_factory()
        r = repr(adapter)
        assert "PSED***" in r  # masked appkey
        assert "1234***" in r  # masked account
        # Full secret must not appear
        assert "A" * 32 not in r
        assert "12345678" not in r


# =============================================================================
# Token issuance + caching
# =============================================================================

class TestTokenLifecycle:
    def test_first_call_issues_token(self, adapter_factory):
        adapter, opener = adapter_factory()
        opener.queue(200, {
            "access_token": "tok-abc123",
            "token_type": "Bearer",
            "expires_in": 86400,
        })
        token, expires = adapter._ensure_access_token()
        assert token == "tok-abc123"
        assert len(opener.calls) == 1
        assert "/oauth2/tokenP" in opener.calls[0]["url"]
        # Body must contain credentials
        body = json.loads(opener.calls[0]["body"])
        assert body["grant_type"] == "client_credentials"
        assert body["appkey"].startswith("PSED")

    def test_second_call_uses_cache(self, adapter_factory):
        adapter, opener = adapter_factory()
        opener.queue(200, {
            "access_token": "tok-xyz",
            "expires_in": 86400,
        })
        adapter._ensure_access_token()
        # Second call — should hit cache, no new HTTP call
        token, _ = adapter._ensure_access_token()
        assert token == "tok-xyz"
        assert len(opener.calls) == 1  # only 1 HTTP call total

    def test_token_file_has_0600_perms(self, adapter_factory, tmp_path):
        if os.name != "posix":
            pytest.skip("POSIX-only")
        adapter, opener = adapter_factory()
        opener.queue(200, {"access_token": "tok-a", "expires_in": 86400})
        adapter._ensure_access_token()
        # Find the token file
        token_files = list(tmp_path.glob(".kis_token_*.json"))
        assert len(token_files) == 1
        mode = token_files[0].stat().st_mode & 0o777
        assert mode == 0o600

    def test_token_failure_raises(self, adapter_factory):
        adapter, opener = adapter_factory()
        opener.queue(401, {"error_description": "invalid appkey"})
        with pytest.raises(KISAdapterError, match="token issuance failed"):
            adapter._ensure_access_token()

    def test_interrupted_blocks_calls(self, adapter_factory):
        adapter, opener = adapter_factory()
        adapter.signal_interrupt()
        opener.queue(200, {"access_token": "x"})
        with pytest.raises(KISAdapterError, match="interrupted"):
            adapter._ensure_access_token()


# =============================================================================
# check_connection
# =============================================================================

class TestCheckConnection:
    def test_success(self, adapter_factory):
        adapter, opener = adapter_factory()
        opener.queue(200, {"access_token": "ok-tok", "expires_in": 3600})
        result = adapter.check_connection()
        assert result.success is True
        assert result.mode == BrokerMode.PAPER
        assert result.token_valid is True
        assert "TLS" in result.tls_version

    def test_failure_does_not_raise(self, adapter_factory):
        adapter, opener = adapter_factory()
        opener.queue(500, {"msg1": "server error"})
        result = adapter.check_connection()
        assert result.success is False
        assert result.error_message is not None
        assert result.token_valid is False

    def test_network_error_does_not_raise(self, adapter_factory):
        adapter, opener = adapter_factory()
        opener.queue_error(urllib.error.URLError("network down"))
        result = adapter.check_connection()
        assert result.success is False


# =============================================================================
# get_account_summary
# =============================================================================

class TestGetAccountSummary:
    def test_success(self, adapter_factory):
        adapter, opener = adapter_factory()
        # Token issuance
        opener.queue(200, {"access_token": "tok", "expires_in": 86400})
        # Balance call
        opener.queue(200, {
            "output1": [],
            "output2": [{
                "dnca_tot_amt": "5000000",
                "tot_evlu_amt": "7500000",
                "ord_psbl_cash": "4800000",
            }],
        })
        summary = adapter.get_account_summary()
        assert summary.cash_balance_krw == Decimal("5000000")
        assert summary.total_equity_krw == Decimal("7500000")
        assert summary.buying_power_krw == Decimal("4800000")
        assert summary.account_id_masked == "1234***"

    def test_uses_paper_tr_id(self, adapter_factory):
        adapter, opener = adapter_factory()
        opener.queue(200, {"access_token": "tok", "expires_in": 86400})
        opener.queue(200, {
            "output1": [],
            "output2": [{"dnca_tot_amt": "0", "tot_evlu_amt": "0",
                         "ord_psbl_cash": "0"}],
        })
        adapter.get_account_summary()
        # Second call (after token) is balance — must use paper TR ID
        balance_call = opener.calls[1]
        assert balance_call["headers"]["Tr_id"] == "VTTC8434R"


# =============================================================================
# get_positions
# =============================================================================

class TestGetPositions:
    def test_returns_positions(self, adapter_factory):
        adapter, opener = adapter_factory()
        opener.queue(200, {"access_token": "tok", "expires_in": 86400})
        opener.queue(200, {
            "output1": [
                {
                    "pdno": "005930",
                    "hldg_qty": "10",
                    "pchs_avg_pric": "70000",
                    "prpr": "71000",
                    "evlu_amt": "710000",
                    "evlu_pfls_amt": "10000",
                },
                {
                    "pdno": "000660",
                    "hldg_qty": "0",  # filtered out
                    "pchs_avg_pric": "0",
                    "prpr": "0",
                    "evlu_amt": "0",
                    "evlu_pfls_amt": "0",
                },
            ],
            "output2": [{}],
        })
        positions = adapter.get_positions()
        assert len(positions) == 1  # zero qty filtered
        assert positions[0].symbol == "005930"
        assert positions[0].quantity == Decimal("10")
        assert positions[0].unrealized_pnl_krw == Decimal("10000")

    def test_handles_empty(self, adapter_factory):
        adapter, opener = adapter_factory()
        opener.queue(200, {"access_token": "tok", "expires_in": 86400})
        opener.queue(200, {"output1": [], "output2": [{}]})
        positions = adapter.get_positions()
        assert positions == ()


# =============================================================================
# get_orders
# =============================================================================

class TestGetOrders:
    def test_filled_order(self, adapter_factory):
        adapter, opener = adapter_factory()
        opener.queue(200, {"access_token": "tok", "expires_in": 86400})
        opener.queue(200, {
            "output1": [{
                "odno": "0000123456",
                "pdno": "005930",
                "sll_buy_dvsn_cd": "02",  # buy
                "ord_dvsn_cd": "00",       # limit
                "ord_qty": "10",
                "tot_ccld_qty": "10",
                "ord_unpr": "70000",
                "avg_prvs": "70000",
                "cncl_yn": "N",
                "ord_dt": "20260507",
                "ord_tmd": "093000",
            }],
        })
        orders = adapter.get_orders()
        assert len(orders) == 1
        o = orders[0]
        assert o.order_id == "0000123456"
        assert o.symbol == "005930"
        assert o.side == OrderSide.BUY
        assert o.status == OrderStatus.FILLED
        assert o.quantity == Decimal("10")
        assert o.filled_quantity == Decimal("10")

    def test_partially_filled(self, adapter_factory):
        adapter, opener = adapter_factory()
        opener.queue(200, {"access_token": "tok", "expires_in": 86400})
        opener.queue(200, {
            "output1": [{
                "odno": "0000123",
                "pdno": "005930",
                "sll_buy_dvsn_cd": "02",
                "ord_dvsn_cd": "00",
                "ord_qty": "10",
                "tot_ccld_qty": "5",
                "ord_unpr": "70000",
                "avg_prvs": "0",
                "cncl_yn": "N",
                "ord_dt": "20260507",
                "ord_tmd": "093000",
            }],
        })
        orders = adapter.get_orders()
        assert orders[0].status == OrderStatus.PARTIALLY_FILLED

    def test_cancelled(self, adapter_factory):
        adapter, opener = adapter_factory()
        opener.queue(200, {"access_token": "tok", "expires_in": 86400})
        opener.queue(200, {
            "output1": [{
                "odno": "0000999",
                "pdno": "005930",
                "sll_buy_dvsn_cd": "02",
                "ord_dvsn_cd": "00",
                "ord_qty": "10",
                "tot_ccld_qty": "0",
                "ord_unpr": "70000",
                "avg_prvs": "0",
                "cncl_yn": "Y",  # cancelled
                "ord_dt": "20260507",
                "ord_tmd": "093000",
            }],
        })
        orders = adapter.get_orders()
        assert orders[0].status == OrderStatus.CANCELLED

    def test_rejects_bad_limit(self, adapter_factory):
        adapter, _ = adapter_factory()
        with pytest.raises(ValueError, match="limit must"):
            adapter.get_orders(limit=0)
        with pytest.raises(ValueError, match="limit must"):
            adapter.get_orders(limit=300)

    def test_status_filter(self, adapter_factory):
        adapter, opener = adapter_factory()
        opener.queue(200, {"access_token": "tok", "expires_in": 86400})
        opener.queue(200, {
            "output1": [
                {"odno": "1", "pdno": "x", "sll_buy_dvsn_cd": "02",
                 "ord_dvsn_cd": "00", "ord_qty": "10", "tot_ccld_qty": "10",
                 "ord_unpr": "100", "avg_prvs": "100", "cncl_yn": "N",
                 "ord_dt": "20260507", "ord_tmd": "093000"},
                {"odno": "2", "pdno": "x", "sll_buy_dvsn_cd": "02",
                 "ord_dvsn_cd": "00", "ord_qty": "10", "tot_ccld_qty": "0",
                 "ord_unpr": "100", "avg_prvs": "0", "cncl_yn": "N",
                 "ord_dt": "20260507", "ord_tmd": "093000"},
            ],
        })
        filled = adapter.get_orders(status=OrderStatus.FILLED)
        assert len(filled) == 1
        assert filled[0].order_id == "1"
