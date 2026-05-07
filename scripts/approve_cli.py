#!/usr/bin/env python3
"""
운영자 승인 CLI (Operator Approve CLI)
=======================================

JCPR Trading System - jcpr-ts-v01
Task 35 v0.1

운영자가 pending 승인을 보고 approve/reject.
(Operator inspects pending approvals and decides.)

사용 (Usage):
    # 모든 pending 조회
    python scripts/approve_cli.py --approval-db data/approvals.sqlite --list

    # 특정 ID 상세 조회
    python scripts/approve_cli.py --approval-db data/approvals.sqlite \\
        --approval-id apv-20260507-a1b2c3d4 --show

    # 승인
    python scripts/approve_cli.py --approval-db data/approvals.sqlite \\
        --approval-id apv-20260507-a1b2c3d4 --approve \\
        --decided-by alice --reason "verified"

    # 거부
    python scripts/approve_cli.py --approval-db data/approvals.sqlite \\
        --approval-id apv-20260507-a1b2c3d4 --reject \\
        --decided-by alice --reason "size too large"

    # JSON 출력
    python scripts/approve_cli.py --approval-db data/approvals.sqlite \\
        --list --json

CLI는 audit log를 작성하지 않음 — store에서 상태만 변경.
audit는 다음번 MCP server에서 조회 시 자동 반영.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from src.mcp_servers import (  # noqa: E402
    ApprovalNotFound,
    ApprovalStateError,
    ApprovalStore,
    ApprovalStoreError,
    SelfApprovalError,
    STATUS_PENDING,
)


def _format_record(rec) -> str:
    """승인 레코드 한 줄 요약."""
    age = ""
    try:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        if rec.status == STATUS_PENDING:
            remaining = (rec.expires_at_utc - now).total_seconds()
            age = f" (만료 {int(remaining)}초)"
    except Exception:  # noqa: BLE001
        pass
    return (
        f"{rec.approval_id} | {rec.status:10s} | "
        f"{rec.action_type:15s} | by={rec.requested_by:15s}"
        f"{age}"
    )


def _print_full(rec) -> None:
    """승인 레코드 상세 출력."""
    print(f"━━━ {rec.approval_id} ━━━")
    print(f"  status:        {rec.status}")
    print(f"  action_type:   {rec.action_type}")
    print(f"  requested_by:  {rec.requested_by}")
    print(f"  requested_at:  {rec.requested_at_utc.isoformat()}")
    print(f"  expires_at:    {rec.expires_at_utc.isoformat()}")
    print(f"  paper_mode:    {rec.paper_mode}")
    print(f"  trace_id:      {rec.trace_id}")
    print(f"  decided_at:    {rec.decided_at_utc.isoformat() if rec.decided_at_utc else '(pending)'}")
    print(f"  decided_by:    {rec.decided_by or '-'}")
    print(f"  decision:      {rec.decision_reason or '-'}")
    print(f"  payload:")
    for k, v in rec.payload.items():
        print(f"    {k}: {v}")
    if rec.execution_result:
        print(f"  execution_result:")
        for k, v in rec.execution_result.items():
            print(f"    {k}: {v}")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="JCPR Operator Approve CLI (Task 35 v0.1)",
    )
    p.add_argument("--approval-db", required=True, help="승인 DB 파일")

    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true", help="pending 목록")
    g.add_argument("--show", action="store_true", help="단일 상세 (--approval-id 필요)")
    g.add_argument("--approve", action="store_true", help="승인")
    g.add_argument("--reject", action="store_true", help="거부")

    p.add_argument("--approval-id", help="대상 approval_id")
    p.add_argument("--decided-by", help="결정자 ID (approve/reject)")
    p.add_argument("--reason", default="", help="결정 사유")
    p.add_argument("--json", action="store_true", help="JSON 출력")
    p.add_argument("--limit", type=int, default=50, help="목록 한도")
    p.add_argument("--all-statuses", action="store_true",
                   help="--list 시 pending 외 상태 포함")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    args = _parse_args(argv)

    if not Path(args.approval_db).exists():
        print(f"❌ approval_db 파일 없음: {args.approval_db}", file=sys.stderr)
        return 1

    store = ApprovalStore(db_path=args.approval_db)

    # ─── --list ───────────────────────────────
    if args.list:
        if args.all_statuses:
            from src.mcp_servers._approval_store import ALL_STATUSES
            records = store.list_by_status(list(ALL_STATUSES), limit=args.limit)
        else:
            records = store.list_pending(limit=args.limit)
        if args.json:
            print(json.dumps(
                [r.to_dict() for r in records],
                ensure_ascii=False, indent=2, default=str,
            ))
        else:
            print(f"━━━ Approvals ({len(records)}) ━━━")
            for r in records:
                print(_format_record(r))
        return 0

    # ─── --show ───────────────────────────────
    if args.show:
        if not args.approval_id:
            print("❌ --approval-id required for --show", file=sys.stderr)
            return 2
        try:
            rec = store.get(args.approval_id)
        except ApprovalNotFound:
            print(f"❌ Not found: {args.approval_id}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(rec.to_dict(), ensure_ascii=False, indent=2, default=str))
        else:
            _print_full(rec)
        return 0

    # ─── --approve / --reject ─────────────────
    if not args.approval_id:
        print("❌ --approval-id required", file=sys.stderr)
        return 2
    if not args.decided_by:
        print("❌ --decided-by required", file=sys.stderr)
        return 2

    try:
        if args.approve:
            rec = store.approve(
                args.approval_id,
                decided_by=args.decided_by,
                reason=args.reason,
            )
            print(f"✅ Approved: {args.approval_id}")
        else:  # --reject
            rec = store.reject(
                args.approval_id,
                decided_by=args.decided_by,
                reason=args.reason,
            )
            print(f"❌ Rejected: {args.approval_id}")
        if args.json:
            print(json.dumps(rec.to_dict(), ensure_ascii=False, indent=2, default=str))
        else:
            _print_full(rec)
        return 0
    except ApprovalNotFound:
        print(f"❌ Not found: {args.approval_id}", file=sys.stderr)
        return 1
    except SelfApprovalError as e:
        print(f"🚨 Self-approval blocked: {e}", file=sys.stderr)
        return 3
    except ApprovalStateError as e:
        print(f"❌ State error: {e}", file=sys.stderr)
        return 4
    except ApprovalStoreError as e:
        print(f"❌ Store error: {e}", file=sys.stderr)
        return 5


if __name__ == "__main__":
    sys.exit(main())
