"""
Kill Switch Tests (5 tests)
Stage 2B Deliverable 1.

Implements the <model> clause requirement:
    "ESC or CTRL-C will terminate the process or on-going transaction
     immediately. This will prevail before new trading is ordered."

Each test verifies one channel of the kill switch and the prevailing
ordering against new-order submission.
"""

from __future__ import annotations

import os
import signal
import threading
import time

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.kill_switch]


def _trading_loop(kis, store, kill_switch, n: int = 20) -> int:
    """Submit up to n orders, checking the kill switch BEFORE each submission.
    Returns the number of orders actually submitted."""
    submitted = 0
    for i in range(n):
        if kill_switch.is_tripped():
            break
        oid = f"loop-{i:03d}"
        kis.submit_order(oid, "005930", "BUY", 1, 70_000.0)
        store.insert_order({
            "client_order_id": oid, "symbol": "005930", "side": "BUY",
            "qty": 1, "limit_price": 70_000.0, "status": "ACCEPTED",
            "submitted_ts": time.time(), "last_update_ts": time.time(),
        })
        submitted += 1
        time.sleep(0.05)  # simulate per-order pacing
    return submitted


# 1 -------------------------------------------------------------------------
def test_ctrl_c_trips_switch_and_stops_loop(kis_paper_client, wal_store, kill_switch) -> None:
    """SIGINT (Ctrl-C) must trip the switch, stopping the order loop mid-flight.

    We register a handler that calls kill_switch.trip(), then raise SIGINT
    from a sibling thread after a short delay.
    """
    prev = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda s, f: kill_switch.trip("SIGINT"))

    def trip_after_delay() -> None:
        time.sleep(0.15)
        os.kill(os.getpid(), signal.SIGINT)

    try:
        threading.Thread(target=trip_after_delay, daemon=True).start()
        submitted = _trading_loop(kis_paper_client, wal_store, kill_switch, n=20)
    finally:
        signal.signal(signal.SIGINT, prev)

    # We expect SOME orders to have gone through before the trip, but not all.
    assert 0 < submitted < 20, f"unexpected submitted count: {submitted}"
    assert kill_switch.is_tripped()


# 2 -------------------------------------------------------------------------
def test_esc_trip_stops_loop(kis_paper_client, wal_store, kill_switch) -> None:
    """The ESC channel is exposed as KillSwitch.trip(); a simulated ESC press
    from a listener thread must stop the loop."""
    def esc_listener() -> None:
        time.sleep(0.12)
        kill_switch.trip("ESC")

    threading.Thread(target=esc_listener, daemon=True).start()
    submitted = _trading_loop(kis_paper_client, wal_store, kill_switch, n=20)
    assert 0 < submitted < 20
    assert kill_switch.is_tripped()


# 3 -------------------------------------------------------------------------
def test_kill_switch_prevails_before_new_order(kis_paper_client, wal_store, kill_switch) -> None:
    """The <model> clause: kill switch must prevail BEFORE new trading is
    ordered. If the switch is already tripped, zero orders go through."""
    kill_switch.trip("pre-tripped")
    submitted = _trading_loop(kis_paper_client, wal_store, kill_switch, n=10)
    assert submitted == 0
    assert wal_store.count("orders") == 0


# 4 -------------------------------------------------------------------------
def test_inflight_state_is_preserved_when_switch_trips(
    kis_paper_client, wal_store, kill_switch
) -> None:
    """When the switch trips, orders already accepted by KIS must remain
    visible in the ledger - we are stopping further submissions, not erasing
    history."""
    # submit two orders manually
    for i, oid in enumerate(["pre-1", "pre-2"]):
        kis_paper_client.submit_order(oid, "005930", "BUY", 1, 70_000.0)
        wal_store.insert_order({
            "client_order_id": oid, "symbol": "005930", "side": "BUY",
            "qty": 1, "limit_price": 70_000.0, "status": "ACCEPTED",
            "submitted_ts": time.time(), "last_update_ts": time.time(),
        })

    kill_switch.trip("manual")
    # subsequent loop submits nothing
    submitted = _trading_loop(kis_paper_client, wal_store, kill_switch, n=5)
    assert submitted == 0

    # but the two pre-trip orders are still in the ledger
    assert wal_store.count("orders") == 2
    assert kis_paper_client.get_order("pre-1") is not None
    assert kis_paper_client.get_order("pre-2") is not None


# 5 -------------------------------------------------------------------------
def test_file_based_kill_switch(kill_switch, kis_paper_client, wal_store) -> None:
    """The third channel: presence of a sentinel file trips the switch.
    This lets an external operator (or another process) stop trading."""
    assert not kill_switch.is_tripped()
    # external process drops the sentinel file
    kill_switch.kill_file.write_text("STOP")

    assert kill_switch.is_tripped()
    submitted = _trading_loop(kis_paper_client, wal_store, kill_switch, n=5)
    assert submitted == 0
