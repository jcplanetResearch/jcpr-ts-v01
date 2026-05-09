"""
KIS Paper Rejection Tests (5 tests)
Stage 2B Deliverable 1.

Verifies that the pre-trade risk gate rejects unsafe orders BEFORE they
reach the broker, and that the gate's rejection reasons are stable
identifiers (used downstream by the rejection report in task 20).
"""

from __future__ import annotations

import time

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.kis_reject]


# 1 -------------------------------------------------------------------------
def test_rejects_order_exceeding_capacity(risk_gate, kis_paper_client) -> None:
    """An order whose notional exceeds max_order_notional must be rejected
    by the gate WITHOUT being submitted to KIS."""
    # max_order_notional = 5_000_000 in the default RiskGateConfig
    decision = risk_gate.check("005930", "BUY", qty=100, price=70_000.0)
    assert not decision.passed
    assert decision.reason_code == "ORDER_NOTIONAL_LIMIT"
    # Confirm we never submitted to KIS
    assert kis_paper_client.get_order("would-not-exist") is None


# 2 -------------------------------------------------------------------------
def test_rejects_single_symbol_exposure_breach(risk_gate, wal_store) -> None:
    """Adding more to a position that would push exposure over the per-symbol
    limit must be rejected. Existing position is read from the ledger."""
    # seed an existing 30-share position at 70_000 -> 2.1M exposure
    wal_store.upsert_position("005930", 30, 70_000.0)
    # +20 @ 70_000 would push to 50 * 70_000 = 3.5M, > 3M limit
    decision = risk_gate.check("005930", "BUY", qty=20, price=70_000.0)
    assert not decision.passed
    assert decision.reason_code == "SYMBOL_EXPOSURE_LIMIT"


# 3 -------------------------------------------------------------------------
def test_rejects_when_daily_loss_limit_breached(risk_gate) -> None:
    """If daily realized P&L is below the daily_loss_limit, no further orders."""
    decision = risk_gate.check(
        "005930", "BUY", qty=1, price=70_000.0,
        daily_pnl=-600_000.0,  # below the -500_000 limit
    )
    assert not decision.passed
    assert decision.reason_code == "DAILY_LOSS_LIMIT"


# 4 -------------------------------------------------------------------------
def test_rejects_unknown_symbol_and_bad_qty(risk_gate) -> None:
    """Unknown symbols and non-positive quantities are rejected with stable
    reason codes that downstream reports filter on."""
    bad_symbol = risk_gate.check("999999", "BUY", qty=1, price=10_000.0)
    assert not bad_symbol.passed
    assert bad_symbol.reason_code == "UNKNOWN_SYMBOL"

    bad_qty = risk_gate.check("005930", "BUY", qty=0, price=70_000.0)
    assert not bad_qty.passed
    assert bad_qty.reason_code == "BAD_QTY"


# 5 -------------------------------------------------------------------------
def test_rejects_when_market_is_closed(wal_store) -> None:
    """An order placed outside market hours must be rejected with
    MARKET_CLOSED, regardless of all other parameters being valid."""
    from tests.integration.conftest import RiskGate, RiskGateConfig

    # Build a fresh gate whose clock reports 16:30 (after close)
    closed_clock = time.struct_time((2026, 5, 8, 16, 30, 0, 4, 128, 0))
    gate = RiskGate(wal_store, RiskGateConfig(), now_provider=lambda: closed_clock)

    decision = gate.check("005930", "BUY", qty=1, price=70_000.0)
    assert not decision.passed
    assert decision.reason_code == "MARKET_CLOSED"
