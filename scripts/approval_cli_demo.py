#!/usr/bin/env python3
"""
승인 워크플로우 CLI 데모 (Approval Workflow CLI Demo)
======================================================

JCPR Trading System - jcpr-ts-v01
Task 40 v0.1

단독 실행 — CLIApprovalProvider 동작을 직접 체험.

사용법 (Usage):
    python scripts/approval_cli_demo.py
    python scripts/approval_cli_demo.py --timeout 30
    python scripts/approval_cli_demo.py --no-strict --quantity 50
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.execution.approval import ApprovalRequest
from src.execution.approval_audit import ApprovalAuditLog
from src.execution.approval_cli import CLIApprovalProvider


def main() -> int:
    parser = argparse.ArgumentParser(
        description="JCPR Approval CLI Demo — Task 40 v0.1",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--symbol", default="005930", help="종목 코드")
    parser.add_argument("--side", choices=["buy", "sell"], default="buy")
    parser.add_argument("--quantity", type=int, default=10)
    parser.add_argument("--price", type=int, default=70500)
    parser.add_argument("--timeout", type=float, default=120.0,
                        help="응답 대기 시간 (초)")
    parser.add_argument("--no-strict", action="store_true",
                        help="명시적 입력 강제 해제 ('y' 단축 허용)")
    parser.add_argument("--live-env", action="store_true",
                        help="is_live_env=True로 표시")
    parser.add_argument("--live-orders", action="store_true",
                        help="is_dry_run=False로 표시 (실 송신 가능)")
    parser.add_argument("--audit-log", type=str, default=None,
                        help="audit log 경로 (지정 시 JSONL 기록)")
    parser.add_argument("--no-color", action="store_true")

    args = parser.parse_args()

    audit = ApprovalAuditLog(args.audit_log) if args.audit_log else None

    provider = CLIApprovalProvider(
        timeout_sec=args.timeout,
        require_explicit_yes=not args.no_strict,
        deny_on_timeout=True,
        audit_log=audit,
        approver_id="cli_demo",
        use_color=not args.no_color,
    )

    quantity = args.quantity
    price = Decimal(str(args.price))
    request = ApprovalRequest(
        execution_id=f"exec-demo-{datetime.now(timezone.utc).strftime('%H%M%S')}",
        signal_id="sig-demo",
        symbol=args.symbol,
        side=args.side,
        quantity=quantity,
        price=price,
        estimated_cost_krw=price * Decimal(quantity),
        is_dry_run=not args.live_orders,
        is_live_env=args.live_env,
        requested_at_utc=datetime.now(timezone.utc),
    )

    decision = provider.request_approval(request)

    print(f"\n--- 결과 (Result) ---")
    print(f"approved: {decision.approved}")
    print(f"reason:   {decision.reason}")
    print(f"approver: {decision.approver}")
    print(f"time:     {decision.decided_at_utc.isoformat()}")
    if audit:
        print(f"audit:    {audit.path}")

    return 0 if decision.approved else 1


if __name__ == "__main__":
    sys.exit(main())
