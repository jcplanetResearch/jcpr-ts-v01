#!/usr/bin/env python3
"""Task 9 — Show today's orders CLI. READ-ONLY.

Usage:
    python scripts/show_orders.py
    python scripts/show_orders.py --status filled
    python scripts/show_orders.py --symbol 005930
    python scripts/show_orders.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Show KIS orders (Task 9)")
    p.add_argument("--prod", action="store_true")
    p.add_argument("--env-path", type=str, default=".env")
    p.add_argument("--token-cache", type=str, default=None)
    p.add_argument("--status", type=str, default=None,
                   choices=["pending", "partially_filled", "filled",
                            "cancelled", "rejected"])
    p.add_argument("--symbol", type=str, default=None)
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--json", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    try:
        from src.brokers import (
            BrokerMode,
            KISAdapterError,
            KISBrokerAdapter,
            OrderStatus,
            SecretLoadError,
            load_kis_secrets,
        )
    except ImportError as e:
        print(f"ERROR: import — {e}", file=sys.stderr)
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

    status_filter = OrderStatus(args.status) if args.status else None

    try:
        orders = adapter.get_orders(
            status=status_filter,
            symbol=args.symbol,
            limit=args.limit,
        )
    except KISAdapterError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if args.json:
        out = [
            {
                "order_id": o.order_id,
                "symbol": o.symbol,
                "side": o.side.value,
                "order_type": o.order_type.value,
                "quantity": str(o.quantity),
                "filled_quantity": str(o.filled_quantity),
                "limit_price_krw": str(o.limit_price_krw) if o.limit_price_krw else None,
                "avg_fill_price_krw": str(o.avg_fill_price_krw) if o.avg_fill_price_krw else None,
                "status": o.status.value,
                "placed_at_utc": o.placed_at_utc.isoformat(),
            }
            for o in orders
        ]
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(f"Orders ({len(orders)}, mode={mode_str}):")
        print("-" * 100)
        print(f"{'OrderID':<14} {'Symbol':<8} {'Side':<5} {'Type':<7} "
              f"{'Qty':>8} {'Filled':>8} {'LimitPx':>10} {'AvgFill':>10} {'Status':<18}")
        print("-" * 100)
        for o in orders:
            print(
                f"{o.order_id:<14} {o.symbol:<8} {o.side.value:<5} "
                f"{o.order_type.value:<7} {o.quantity:>8,} {o.filled_quantity:>8,} "
                f"{(o.limit_price_krw or 0):>10,} {(o.avg_fill_price_krw or 0):>10,} "
                f"{o.status.value:<18}"
            )
        print("-" * 100)
        if not orders:
            print("(no orders)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
