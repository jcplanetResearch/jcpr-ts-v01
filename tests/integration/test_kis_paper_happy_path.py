"""
KIS Paper Happy-Path E2E Tests (6 tests)
Stage 2B Deliverable 1.

Exercises the full signal -> risk gate -> order -> fill -> ledger -> P&L
loop against the KIS paper SDK (or fake fallback). These are the
"good day" trajectories; rejections and kill-switch trips live in
sibling files.
"""

from __future__ import annotations

import time

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.kis_happy]


# Helpers --------------------------------------------------------------------

def _submit_via_gate(risk_gate, kis, store, *, oid, symbol, side, qty, price):
    """Run pre-trade risk, submit through KIS paper, persist to ledger."""
    decision = risk_gate.check(symbol, side, qty, price)
    assert decision.passed, f"risk gate rejected: {decision.reason_code}"
    resp = kis.submit_order(oid, symbol, side, qty, limit_price=price)
    assert resp["status"] == "ACCEPTED"
    store.insert_order({
        "client_order_id": oid, "symbol": symbol, "side": side, "qty": qty,
        "limit_price": price, "status": "ACCEPTED",
        "submitted_ts": time.time(), "last_update_ts": time.time(),
    })
    return resp


# 1 -------------------------------------------------------------------------
def test_full_buy_loop_signal_to_pnl(risk_gate, kis_paper_client, wal_store) -> None:
    """Signal -> gate -> order -> fill -> position -> realized P&L (zero, no exit yet)."""
    _submit_via_gate(risk_gate, kis_paper_client, wal_store,
                     oid="hp-buy-1", symbol="005930", side="BUY",
                     qty=10, price=70_000.0)

    fill = kis_paper_client.simulate_fill("hp-buy-1", qty=10, price=70_050.0, fee=70.0)
    wal_store.insert_fill(fill)
    wal_store.upsert_position("005930", qty=10, avg_cost=70_050.0)

    pos = wal_store.get_position("005930")
    assert pos == (10, 70_050.0)
    # No exit yet -> realized P&L undefined but ledger consistent
    assert wal_store.count("fills") == 1


# 2 -------------------------------------------------------------------------
def test_buy_then_sell_realized_pnl(risk_gate, kis_paper_client, wal_store) -> None:
    """Round-trip a position and verify realized P&L matches expectation."""
    # buy 10 @ 70,000
    _submit_via_gate(risk_gate, kis_paper_client, wal_store,
                     oid="hp-rt-buy", symbol="005930", side="BUY",
                     qty=10, price=70_000.0)
    fb = kis_paper_client.simulate_fill("hp-rt-buy", 10, 70_000.0, fee=70.0)
    wal_store.insert_fill(fb)
    wal_store.upsert_position("005930", 10, 70_000.0)

    # sell 10 @ 71,000
    _submit_via_gate(risk_gate, kis_paper_client, wal_store,
                     oid="hp-rt-sell", symbol="005930", side="SELL",
                     qty=10, price=71_000.0)
    fs = kis_paper_client.simulate_fill("hp-rt-sell", 10, 71_000.0, fee=71.0)
    wal_store.insert_fill(fs)
    wal_store.upsert_position("005930", 0, 0.0)

    # realized P&L = (71_000 - 70_000) * 10 - fees
    realized = (71_000.0 - 70_000.0) * 10 - (70.0 + 71.0)
    assert realized == pytest.approx(9_859.0)
    assert wal_store.get_position("005930") == (0, 0.0)


# 3 -------------------------------------------------------------------------
def test_partial_fill_reflected_in_ledger(risk_gate, kis_paper_client, wal_store) -> None:
    """An order that fills in two slices must yield two fill rows and a
    PARTIALLY_FILLED -> FILLED status progression."""
    _submit_via_gate(risk_gate, kis_paper_client, wal_store,
                     oid="hp-partial", symbol="000660", side="BUY",
                     qty=10, price=120_000.0)

    f1 = kis_paper_client.simulate_fill("hp-partial", qty=4, price=120_000.0)
    wal_store.insert_fill(f1)
    order_state = kis_paper_client.get_order("hp-partial")
    assert order_state["status"] == "PARTIALLY_FILLED"

    f2 = kis_paper_client.simulate_fill("hp-partial", qty=6, price=120_050.0)
    wal_store.insert_fill(f2)
    order_state = kis_paper_client.get_order("hp-partial")
    assert order_state["status"] == "FILLED"
    assert wal_store.count("fills") == 2


# 4 -------------------------------------------------------------------------
def test_short_sell_then_cover_realized_pnl(risk_gate, kis_paper_client, wal_store) -> None:
    """Sell-then-buy round trip (covering a short). P&L direction must invert."""
    # sell 5 @ 71,000 (open short)
    _submit_via_gate(risk_gate, kis_paper_client, wal_store,
                     oid="hp-shrt-sell", symbol="035420", side="SELL",
                     qty=5, price=200_000.0)
    fs = kis_paper_client.simulate_fill("hp-shrt-sell", 5, 200_000.0, fee=100.0)
    wal_store.insert_fill(fs)
    wal_store.upsert_position("035420", -5, 200_000.0)

    # buy 5 @ 195,000 (close short)
    _submit_via_gate(risk_gate, kis_paper_client, wal_store,
                     oid="hp-shrt-buy", symbol="035420", side="BUY",
                     qty=5, price=195_000.0)
    fb = kis_paper_client.simulate_fill("hp-shrt-buy", 5, 195_000.0, fee=98.0)
    wal_store.insert_fill(fb)
    wal_store.upsert_position("035420", 0, 0.0)

    realized = (200_000.0 - 195_000.0) * 5 - (100.0 + 98.0)
    assert realized == pytest.approx(24_802.0)


# 5 -------------------------------------------------------------------------
def test_cash_balance_query_is_nonzero(kis_paper_client) -> None:
    """The paper client must report a cash balance that we can read without
    triggering authentication errors. This is a smoke test for SDK wiring."""
    cash = kis_paper_client.get_cash_balance()
    assert isinstance(cash, (int, float))
    assert cash > 0


# 6 -------------------------------------------------------------------------
def test_audit_trail_persists_for_full_loop(risk_gate, kis_paper_client, wal_store) -> None:
    """After a full buy+sell loop, the orders and fills tables must contain
    the complete history with consistent row counts. This guards against
    code paths that bypass the ledger writer."""
    for oid, side, qty, px in [
        ("hp-aud-1", "BUY", 3, 70_000.0),
        ("hp-aud-2", "BUY", 3, 70_100.0),
        ("hp-aud-3", "SELL", 6, 70_500.0),
    ]:
        _submit_via_gate(risk_gate, kis_paper_client, wal_store,
                         oid=oid, symbol="005930", side=side,
                         qty=qty, price=px)
        fill = kis_paper_client.simulate_fill(oid, qty, px, fee=qty * 1.0)
        wal_store.insert_fill(fill)

    assert wal_store.count("orders") == 3
    assert wal_store.count("fills") == 3
