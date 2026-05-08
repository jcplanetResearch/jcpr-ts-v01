#!/usr/bin/env python3
"""Task 9 — Show current positions CLI.

Fetches account summary + positions from KIS. READ-ONLY.

Usage:
    python scripts/show_positions.py
    JCPR_ALLOW_LIVE=1 python scripts/show_positions.py --prod
    python scripts/show_positions.py --json

Exit codes:
    0  Success
    1  Fetch failed
    2  Configuration error
    3  Unsafe mode without env var
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from decimal import Decimal
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Show KIS positions (Task 9)")
    p.add_argument("--prod", action="store_true")
    p.add_argument("--env-path", type=str, default=".env")
    p.add_argument("--token-cache", type=str, default=None)
    p.add_argument("--json", action="store_true",
                   help="Output as JSON instead of table")
    return p.parse_args()


def _format_table(summary, positions) -> str:
    lines = []
    lines.append("=" * 80)
    lines.append(f"Account: {summary.account_id_masked}  Mode: {summary.mode.value}")
    lines.append("=" * 80)
    lines.append(f"Cash Balance:    {summary.cash_balance_krw:>20,} KRW")
    lines.append(f"Total Equity:    {summary.total_equity_krw:>20,} KRW")
    lines.append(f"Buying Power:    {summary.buying_power_krw:>20,} KRW")
    lines.append("")

    if not positions:
        lines.append("No open positions.")
    else:
        lines.append(f"Positions ({len(positions)}):")
        lines.append("-" * 80)
        lines.append(f"{'Symbol':<10} {'Qty':>10} {'AvgCost':>12} "
                     f"{'Current':>12} {'MktValue':>15} {'UnrealizedP&L':>15}")
        lines.append("-" * 80)
        for p in positions:
            lines.append(
                f"{p.symbol:<10} {p.quantity:>10,} {p.avg_cost_krw:>12,} "
                f"{p.current_price_krw:>12,} {p.market_value_krw:>15,} "
                f"{p.unrealized_pnl_krw:>15,}"
            )
    lines.append("=" * 80)
    return "\n".join(lines)


def main() -> int:
    args = parse_args()

    try:
        from src.brokers import (
            BrokerMode,
            KISAdapterError,
            KISBrokerAdapter,
            SecretLoadError,
            load_kis_secrets,
        )
    except ImportError as e:
        print(f"ERROR: import failed — {e}", file=sys.stderr)
        return 2

    mode_str = "prod" if args.prod else "paper"
    mode = BrokerMode.PROD if args.prod else BrokerMode.PAPER

    if args.prod and os.environ.get("JCPR_ALLOW_LIVE") != "1":
        print("ERROR: --prod requires JCPR_ALLOW_LIVE=1", file=sys.stderr)
        return 3

    try:
        secrets = load_kis_secrets(env_path=args.env_path, mode=mode_str)
        adapter = KISBrokerAdapter(
            secrets=secrets,
            mode=mode,
            token_cache_path=args.token_cache,
        )
    except (SecretLoadError, Exception) as e:
        print(f"ERROR: setup — {e}", file=sys.stderr)
        return 2

    def _on_signal(signum, frame):
        adapter.signal_interrupt()
        sys.exit(130)
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    try:
        summary = adapter.get_account_summary()
        positions = adapter.get_positions()
    except KISAdapterError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if args.json:
        out = {
            "account": {
                "account_id_masked": summary.account_id_masked,
                "cash_balance_krw": str(summary.cash_balance_krw),
                "total_equity_krw": str(summary.total_equity_krw),
                "buying_power_krw": str(summary.buying_power_krw),
                "mode": summary.mode.value,
                "fetched_at_utc": summary.fetched_at_utc.isoformat(),
            },
            "positions": [
                {
                    "symbol": p.symbol,
                    "quantity": str(p.quantity),
                    "avg_cost_krw": str(p.avg_cost_krw),
                    "current_price_krw": str(p.current_price_krw),
                    "market_value_krw": str(p.market_value_krw),
                    "unrealized_pnl_krw": str(p.unrealized_pnl_krw),
                }
                for p in positions
            ],
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(_format_table(summary, positions))

    return 0


if __name__ == "__main__":
    sys.exit(main())
