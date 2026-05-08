#!/usr/bin/env python3
"""Task 40 — Operator approval CLI.

Lists pending proposals, shows details, allows approve/reject decisions.

CRITICAL SAFETY RULES:
    1. Operator's actor id MUST differ from the proposal's requested_by.
       Self-approval is blocked at the store level.
    2. Approval gives green light but does NOT execute. ExecutionGateway
       handles execution after approval.
    3. Each decision requires explicit confirmation (y/N) and reason text.
    4. Never run this script as the same user that runs trading agents.

Usage:
    # List pending proposals
    python scripts/approve_cli.py list

    # View a specific proposal
    python scripts/approve_cli.py show ap-uuid

    # Approve a proposal (interactive)
    python scripts/approve_cli.py approve ap-uuid --actor operator-jcpr

    # Reject with reason
    python scripts/approve_cli.py reject ap-uuid --actor operator-jcpr \\
        --reason "위험도 초과 — 포지션 한도 도달"

Exit codes:
    0  Success
    1  Operation failed
    2  Invalid arguments
    3  Self-approval blocked
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO))


def _format_proposal(p) -> str:
    """Render proposal as human-readable text."""
    lines = []
    lines.append("=" * 70)
    lines.append(f"Approval ID:   {p.approval_id}")
    lines.append(f"Action Type:   {p.action_type}")
    lines.append(f"State:         {p.state.value}")
    lines.append(f"Requested By:  {p.requested_by}")
    lines.append(f"Proposed:      {p.proposed_at_utc.isoformat()}")
    lines.append(f"Expires:       {p.expires_at_utc.isoformat()}")
    if p.decided_by:
        lines.append(f"Decided By:    {p.decided_by}")
        lines.append(f"Decided At:    {p.decided_at_utc.isoformat()}")
    if p.decision_reason_kr:
        lines.append(f"Reason:        {p.decision_reason_kr}")
    lines.append("")
    lines.append("Payload:")
    for k, v in dict(p.payload).items():
        lines.append(f"  {k:<22} {v}")
    if p.execution_result:
        lines.append("")
        lines.append("Execution Result:")
        for k, v in dict(p.execution_result).items():
            lines.append(f"  {k:<22} {v}")
    lines.append("=" * 70)
    return "\n".join(lines)


def _list_pending(store) -> int:
    pending = store.list_pending(limit=50)
    if not pending:
        print("(no pending proposals)")
        return 0
    print(f"Pending proposals ({len(pending)}):")
    print("-" * 90)
    print(f"{'Approval ID':<42} {'State':<10} {'Action':<18} {'Requested By':<15}")
    print("-" * 90)
    for p in pending:
        print(f"{p.approval_id:<42} {p.state.value:<10} "
              f"{p.action_type:<18} {p.requested_by:<15}")
    print("-" * 90)
    return 0


def _show(store, approval_id: str) -> int:
    try:
        p = store.get(approval_id)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(_format_proposal(p))
    return 0


def _confirm(prompt: str) -> bool:
    """Interactive y/N confirmation. Treats EOF/Ctrl-D as no."""
    try:
        response = input(f"{prompt} [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return response in ("y", "yes")


def _approve(store, approval_id: str, actor: str,
             reason: str | None, no_confirm: bool) -> int:
    from src.execution._approval_state import (
        ApprovalState,
        SelfApprovalError,
        StateTransitionError,
    )

    try:
        p = store.get(approval_id)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print(_format_proposal(p))
    print()
    print(f"⚠️  You are about to APPROVE this proposal as '{actor}'.")
    print(f"⚠️  This authorizes the agent to execute the action.")
    print()

    if p.state != ApprovalState.PROPOSED:
        print(f"ERROR: cannot approve — state is {p.state.value}",
              file=sys.stderr)
        return 1

    if p.requested_by == actor:
        print("ERROR: self-approval blocked — actor is same as requested_by",
              file=sys.stderr)
        return 3

    if not no_confirm and not _confirm("Confirm APPROVE?"):
        print("Aborted.")
        return 1

    try:
        result = store.transition(
            approval_id=approval_id,
            target_state=ApprovalState.APPROVED,
            actor=actor,
            reason_kr=reason,
        )
    except SelfApprovalError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 3
    except StateTransitionError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print(f"✓ Approved {approval_id} by {actor}")
    print(f"  State: {result.state.value}")
    return 0


def _reject(store, approval_id: str, actor: str,
            reason: str, no_confirm: bool) -> int:
    from src.execution._approval_state import (
        ApprovalState,
        SelfApprovalError,
        StateTransitionError,
    )

    if not reason:
        print("ERROR: --reason required for rejection", file=sys.stderr)
        return 2

    try:
        p = store.get(approval_id)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print(_format_proposal(p))
    print()
    print(f"You are about to REJECT this proposal as '{actor}'.")
    print(f"Reason: {reason}")

    if p.requested_by == actor:
        print("ERROR: self-rejection blocked", file=sys.stderr)
        return 3

    if not no_confirm and not _confirm("Confirm REJECT?"):
        print("Aborted.")
        return 1

    try:
        result = store.transition(
            approval_id=approval_id,
            target_state=ApprovalState.REJECTED,
            actor=actor,
            reason_kr=reason,
        )
    except SelfApprovalError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 3
    except StateTransitionError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print(f"✗ Rejected {approval_id} by {actor}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="JCPR Trading System — Operator Approval CLI (Task 40)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default=os.environ.get("JCPR_APPROVAL_DB", "./runtime/approvals.db"),
        help="Path to approval SQLite DB",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List pending proposals")

    show_p = sub.add_parser("show", help="Show a specific proposal")
    show_p.add_argument("approval_id")

    approve_p = sub.add_parser("approve", help="Approve a proposal")
    approve_p.add_argument("approval_id")
    approve_p.add_argument("--actor", required=True,
                           help="Operator id (must differ from requested_by)")
    approve_p.add_argument("--reason", default=None,
                           help="Optional approval reason in Korean")
    approve_p.add_argument("--yes", "-y", action="store_true",
                           dest="no_confirm",
                           help="Skip interactive confirmation")

    reject_p = sub.add_parser("reject", help="Reject a proposal")
    reject_p.add_argument("approval_id")
    reject_p.add_argument("--actor", required=True)
    reject_p.add_argument("--reason", required=True,
                          help="Rejection reason (Korean)")
    reject_p.add_argument("--yes", "-y", action="store_true",
                          dest="no_confirm")

    args = parser.parse_args()

    try:
        from src.execution._approval_state import ApprovalStore
    except ImportError as e:
        print(f"ERROR: import — {e}", file=sys.stderr)
        return 2

    db_path = Path(args.db_path)
    if not db_path.parent.exists():
        db_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        store = ApprovalStore(db_path=db_path)
    except Exception as e:
        print(f"ERROR: cannot open DB — {e}", file=sys.stderr)
        return 2

    if args.command == "list":
        return _list_pending(store)
    elif args.command == "show":
        return _show(store, args.approval_id)
    elif args.command == "approve":
        return _approve(store, args.approval_id, args.actor,
                        args.reason, args.no_confirm)
    elif args.command == "reject":
        return _reject(store, args.approval_id, args.actor,
                       args.reason, args.no_confirm)
    else:
        parser.print_help()
        return 2


if __name__ == "__main__":
    sys.exit(main())
