#!/usr/bin/env python3
"""Task 9 — Broker connection check CLI.

Verifies KIS API connectivity, TLS, token validity. NEVER places orders.

Usage:
    # Default — paper mode
    python scripts/check_broker_connection.py

    # Production mode (requires JCPR_ALLOW_LIVE=1)
    JCPR_ALLOW_LIVE=1 python scripts/check_broker_connection.py --prod

    # Custom .env path
    python scripts/check_broker_connection.py --env-path /path/to/.env

Exit codes:
    0  Connection OK
    1  Connection failed
    2  Configuration / secret loading error
    3  Unsafe mode without env var
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
from pathlib import Path


# Path setup
_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Check KIS broker connection (Task 9)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--prod", action="store_true",
                   help="Use production mode (requires JCPR_ALLOW_LIVE=1)")
    p.add_argument("--env-path", type=str, default=".env",
                   help="Path to .env file (default: .env)")
    p.add_argument("--token-cache", type=str, default=None,
                   help="Path to token cache file")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    try:
        from src.brokers import (
            BrokerMode,
            KISBrokerAdapter,
            SecretLoadError,
            load_kis_secrets,
        )
    except ImportError as e:
        print(f"ERROR: import failed — {e}", file=sys.stderr)
        return 2

    mode_str = "prod" if args.prod else "paper"
    mode = BrokerMode.PROD if args.prod else BrokerMode.PAPER

    # Defense in depth — refuse prod without env var
    if args.prod and os.environ.get("JCPR_ALLOW_LIVE") != "1":
        print("ERROR: --prod requires JCPR_ALLOW_LIVE=1 environment variable.",
              file=sys.stderr)
        print("This is a safety guard. Refusing to connect to live API.",
              file=sys.stderr)
        return 3

    # Load secrets
    try:
        secrets = load_kis_secrets(env_path=args.env_path, mode=mode_str)
    except SecretLoadError as e:
        print(f"ERROR: secret loading failed — {e}", file=sys.stderr)
        return 2

    print(f"Loaded secrets for mode={mode_str}")
    print(f"  account: {secrets.account_masked}")
    print(f"  appkey:  {secrets.appkey_masked}")
    print()

    # Build adapter
    try:
        adapter = KISBrokerAdapter(
            secrets=secrets,
            mode=mode,
            token_cache_path=args.token_cache,
        )
    except Exception as e:
        print(f"ERROR: adapter construction — {e}", file=sys.stderr)
        return 2

    # Hook ESC/Ctrl-C
    def _on_signal(signum, frame):
        print("\nReceived signal — aborting...", file=sys.stderr)
        adapter.signal_interrupt()
        sys.exit(130)
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    # Run check
    print(f"Checking connection to {adapter.base_url} ...")
    result = adapter.check_connection()

    print()
    print("=" * 60)
    print(f"Status:        {'✓ OK' if result.success else '✗ FAILED'}")
    print(f"Mode:          {result.mode.value}")
    print(f"Base URL:      {result.base_url}")
    print(f"TLS Version:   {result.tls_version}")
    print(f"Token Valid:   {result.token_valid}")
    if result.token_expires_at_utc:
        print(f"Token Expires: {result.token_expires_at_utc.isoformat()}")
    if result.server_time_utc:
        print(f"Server Time:   {result.server_time_utc.isoformat()}")
    print(f"Elapsed:       {result.elapsed_ms}ms")
    if result.error_message:
        print(f"Error:         {result.error_message}")
    print("=" * 60)

    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
