#!/usr/bin/env python3
"""
MCP Restricted 서버 stdio 진입점
=================================

JCPR Trading System - jcpr-ts-v01
Task 35 v0.1
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


def main() -> int:
    try:
        from src.mcp_servers import (
            build_restricted_server,
            load_restricted_config_from_env,
        )
    except ImportError as e:
        print(f"❌ Import error: {e}", file=sys.stderr)
        return 1

    try:
        config = load_restricted_config_from_env()
    except ValueError as e:
        print(f"❌ Config error: {e}", file=sys.stderr)
        return 2

    print(
        f"[jcpr-restricted] Starting stdio server\n"
        f"  session={config.session_id}\n"
        f"  operator={config.operator_id}\n"
        f"  audit={config.audit_dir}\n"
        f"  approval_db={config.approval_db}\n"
        f"  allow_live={config.allow_live}",
        file=sys.stderr,
    )

    try:
        server, _store = build_restricted_server(config)
        server.run(transport="stdio")
        return 0
    except KeyboardInterrupt:
        print("[jcpr-restricted] Shutdown requested", file=sys.stderr)
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"❌ Server error: {type(e).__name__}: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
