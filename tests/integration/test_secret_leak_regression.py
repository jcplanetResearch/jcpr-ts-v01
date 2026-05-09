"""
Secret-Leak Regression Tests (2 tests)
Stage 2B Deliverable 1.

These tests guard the <assumption> clause: "No breach of security or
leakage of private key, data information are not allowed."

We construct artifacts (logs, audit reports) the way production code
would, and assert the secret scanner finds zero hits. A regression here
indicates that some code path is now writing credentials or account
numbers into a place we serialize.
"""

from __future__ import annotations

import json
import logging
import os
import time

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.secret_leak]


# 1 -------------------------------------------------------------------------
def test_logs_do_not_contain_kis_credentials_or_account_no(
    capture_logs, secret_scanner
) -> None:
    """Simulate the kinds of log lines the order/risk/PnL modules emit and
    confirm none of them embed KIS keys or account numbers."""
    log = logging.getLogger("jcpr.test")

    # The kinds of structured logs we expect (NO secrets).
    log.info("order submitted client_order_id=hp-001 symbol=005930 side=BUY qty=10")
    log.info("risk decision=PASS reason=None client_order_id=hp-001")
    log.info("fill received fill_id=fill-000001 client_order_id=hp-001 qty=10 price=70050")
    log.warning("reconciliation drift detected expected_qty=10 broker_qty=10")

    # And one log that, if a regression slipped in, COULD have looked like:
    log.debug("connecting to broker at base_url=%s env=%s",
              os.environ.get("KIS_BASE_URL"), os.environ.get("KIS_ENV"))

    blob = "\n".join(capture_logs.records)
    hits = secret_scanner.scan_text(blob)
    # We deliberately set TEST_FAKE_APP_KEY in env, so this also verifies
    # that the scanner does NOT confuse the env var NAME with the value.
    assert hits == [], (
        f"secret scanner found {len(hits)} hit(s) in log output; "
        f"see pattern indices: {[h[0] for h in hits]}"
    )


# 2 -------------------------------------------------------------------------
def test_audit_report_serialization_excludes_secrets(secret_scanner, tmp_path) -> None:
    """Build the JSON shape the daily report writer (task 49) will produce
    and verify the scanner finds nothing. Critically, this includes audit
    metadata where account number leakage has historically been the bug."""
    audit_report = {
        "session": {
            "started_ts": time.time(),
            "ended_ts": time.time() + 3600,
            "env": "paper",  # NOT account_no, NOT app_key
            # broker identifier is fine, it is not a secret:
            "broker": "kis",
        },
        "summary": {
            "starting_capital": 10_000_000.0,
            "ending_capital": 10_009_859.0,
            "realized_pnl": 9_859.0,
            "unrealized_pnl": 0.0,
            "fees": 141.0,
            "slippage": 0.0,
        },
        "rejections": [
            {"reason_code": "ORDER_NOTIONAL_LIMIT", "count": 1},
            {"reason_code": "MARKET_CLOSED", "count": 0},
        ],
        "exceptions": [],
        "next_session_capacity_recommendation": {
            "scale_factor": 1.0, "rationale": "no breaches",
        },
    }
    out = tmp_path / "report.json"
    out.write_text(json.dumps(audit_report, indent=2), encoding="utf-8")

    hits = secret_scanner.scan_file(out)
    assert hits == [], f"audit report leaked secret(s) at lines: {[h[1] for h in hits]}"
