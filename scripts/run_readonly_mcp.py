#!/usr/bin/env python3
"""
MCP Read-Only 서버 stdio 진입점
================================

JCPR Trading System - jcpr-ts-v01
Task 34 v0.1

stdio 모드로 MCP 서버 실행.
(Runs the MCP server in stdio mode.)

사용 (Usage):
    # 환경변수 설정 후 실행
    export JCPR_AUDIT_DIR="data/audit"
    export JCPR_POSITIONS_DB="data/positions.sqlite"
    export JCPR_STRATEGY_REGISTRY="configs/strategy_registry.yaml"
    export JCPR_SESSION_ID="session-2026-05-07"
    python scripts/run_readonly_mcp.py

    # 또는 wrapper script
    bash scripts/run_readonly_mcp.sh

    # MCP client 측 (예: Claude Desktop config):
    {
      "mcpServers": {
        "jcpr-readonly": {
          "command": "python",
          "args": ["/path/to/jcpr-ts-v01/scripts/run_readonly_mcp.py"],
          "env": { ... }
        }
      }
    }
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# repo path
_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


def main() -> int:
    """Run the MCP server in stdio mode."""
    try:
        from src.mcp_servers import build_server, load_config_from_env
    except ImportError as e:
        print(f"❌ Import error: {e}", file=sys.stderr)
        return 1

    try:
        config = load_config_from_env()
    except ValueError as e:
        print(f"❌ Config error: {e}", file=sys.stderr)
        return 2

    print(
        f"[jcpr-readonly] Starting stdio server "
        f"(session={config.session_id}, audit={config.audit_dir})",
        file=sys.stderr,
    )

    try:
        server = build_server(config)
        # FastMCP는 .run() 동기 메서드 + .run_stdio_async() 비동기 모두 제공
        # stdio 모드는 run() 또는 run_stdio_async() 사용 가능
        server.run(transport="stdio")
        return 0
    except KeyboardInterrupt:
        print("[jcpr-readonly] Shutdown requested", file=sys.stderr)
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"❌ Server error: {type(e).__name__}: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
