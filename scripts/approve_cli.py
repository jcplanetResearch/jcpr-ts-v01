#!/usr/bin/env python3
"""approve_cli.py — Unified operator approval CLI (Phase 2).

Replaces the prior two separate scripts:
  - scripts/approve_cli.py (Task 35 — MCP-side, used approvals_mcp.sqlite)
  - scripts/exec_approve_cli.py (Task 40 — Gateway-side, used approvals_exec.sqlite)

Both versions used distinct ApprovalStore implementations against separate
DBs. Phase 1 introduced the unified ApprovalStore. Phase 2 replaces the two
CLI binaries with this single tool, which talks to one DB at the path
specified by JCPR_APPROVAL_DB (default: data/approvals.sqlite).

Subcommands:
  list        List pending (PROPOSED) approvals
  show        Show full detail of one approval (incl. payload)
  approve     Approve a PROPOSED action (transitions PROPOSED -> APPROVED)
  reject      Reject a PROPOSED action
  cancel      Cancel a still-PROPOSED action (requester-side abort)
  history     Show recent decisions (last N approvals)

Security guards:
  - Self-approval blocked at store level (--actor must differ from requester)
  - Live mode confirmations require explicit --yes-i-mean-live
  - All actions are audit-logged via the unified ApprovalStore
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def _import_store():
    """Lazy import — keeps `--help` fast and decouples test setup."""
    from src.execution.approval_store import ApprovalState, ApprovalStore, ApprovalStoreError
    from src.execution._action_kind import ActionKind

    # Phase 1 exception aliases
    try:
        from src.execution.approval_store import ApprovalExpiredError as ExpiredApprovalError
    except ImportError:
        ExpiredApprovalError = ApprovalStoreError  # type: ignore
    try:
        from src.execution.approval_store import SelfApprovalError
    except ImportError:
        SelfApprovalError = ApprovalStoreError  # type: ignore

    return (
        ApprovalState,
        ApprovalStore,
        ApprovalStoreError,
        ExpiredApprovalError,
        SelfApprovalError,
    )


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

# Color helpers — auto-disable when stdout is not a TTY or NO_COLOR is set
def _use_color() -> bool:
    if os.getenv("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def _c(text: str, code: str) -> str:
    if not _use_color():
        return text
    return f"\033[{code}m{text}\033[0m"


def _green(text: str) -> str: return _c(text, "32")
def _yellow(text: str) -> str: return _c(text, "33")
def _red(text: str) -> str: return _c(text, "31")
def _blue(text: str) -> str: return _c(text, "34")
def _bold(text: str) -> str: return _c(text, "1")


def _format_state(state_value: str) -> str:
    if state_value == "proposed":
        return _yellow(state_value)
    if state_value == "approved":
        return _blue(state_value)
    if state_value == "executed":
        return _green(state_value)
    if state_value in ("REJECTED", "EXEC_FAILED", "EXPIRED", "CANCELLED"):
        return _red(state_value)
    return state_value


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def cmd_list(args: argparse.Namespace) -> int:
    ApprovalState, ApprovalStore, *_ = _import_store()
    store = _open_store(args)
    try:
        records = store.list_by_state(ApprovalState.PROPOSED, limit=args.limit)
        if not records:
            print("(no pending approvals)")
            return 0
        print(f"{_bold('Pending approvals')} (count={len(records)}):")
        print()
        for r in records:
            ttl = ""
            if r.expires_at:
                remaining = (r.expires_at - datetime.now(timezone.utc)).total_seconds()
                ttl = f" expires_in={int(remaining)}s" if remaining > 0 else " EXPIRED"
            print(
                f"  {_bold(r.approval_id)}  "
                f"kind={r.action_kind if isinstance(r.action_kind, str) else r.action_kind.value}  "
                f"by={r.requested_by}  "
                f"mode={r.mode}{ttl}"
            )
        return 0
    finally:
        getattr(store, "close", lambda: None)()


def cmd_show(args: argparse.Namespace) -> int:
    _, ApprovalStore, *_ = _import_store()
    store = _open_store(args)
    try:
        try:
            record = store.get(args.approval_id)
        except Exception as exc:
            if "not found" in str(exc).lower():
                print(_red(f"approval not found: {args.approval_id}"), file=sys.stderr)
                return 2
            raise
        d = {
            "approval_id": record.approval_id,
            "state": record.state.value,
            "action_kind": record.action_kind if isinstance(record.action_kind, str) else record.action_kind.value,
            "requested_by": record.requested_by,
            "decided_by": record.decided_by,
            "mode": record.mode,
            "created_at_utc": record.created_at.isoformat(),
            "expires_at_utc": (
                record.expires_at.isoformat()
                if record.expires_at else None
            ),
            "decided_at_utc": (
                record.decided_at.isoformat()
                if record.decided_at else None
            ),
            "decision_reason": record.decision_reason,
            "action_payload": dict(getattr(record, "payload", None) or getattr(record, "action_payload", {})),
            "execution_payload": (
                dict(v) if (v := (getattr(record, "execution_result", None) or getattr(record, "execution_payload", None))) else None
            ),
        }
        print(json.dumps(d, indent=2, ensure_ascii=False))
        return 0
    finally:
        getattr(store, "close", lambda: None)()


def cmd_approve(args: argparse.Namespace) -> int:
    (
        ApprovalState, ApprovalStore, ApprovalStoreError,
        ExpiredApprovalError, SelfApprovalError,
    ) = _import_store()
    store = _open_store(args)
    try:
        try:
            record = store.get(args.approval_id)
        except Exception as exc:
            if "not found" in str(exc).lower():
                print(_red(f"approval not found: {args.approval_id}"), file=sys.stderr)
                return 2
            raise
        if record.state != ApprovalState.PROPOSED:
            print(
                _red(
                    f"cannot approve: state is {record.state.value} "
                    f"(only PROPOSED is approvable)"
                ),
                file=sys.stderr,
            )
            return 3

        # Live-mode safeguard: require explicit confirmation flag
        if record.mode == "live" and not args.yes_i_mean_live:
            print(
                _red(
                    "REFUSED: this approval is for LIVE mode. "
                    "Re-run with --yes-i-mean-live to confirm."
                ),
                file=sys.stderr,
            )
            return 4

        # Interactive confirmation unless --yes given
        if not args.yes:
            _print_record_summary(record)
            resp = input(_yellow("Approve this action? [y/N]: ")).strip().lower()
            if resp != "y":
                print("aborted")
                return 5

        try:
            store.approve(
                args.approval_id,
                decided_by=args.actor,
                reason=args.comment,
            )
        except SelfApprovalError as exc:
            print(_red(f"self-approval blocked: {exc}"), file=sys.stderr)
            return 6
        except ExpiredApprovalError as exc:
            print(_red(f"approval expired: {exc}"), file=sys.stderr)
            return 7
        except ApprovalStoreError as exc:
            print(_red(f"store error: {exc}"), file=sys.stderr)
            return 8

        print(_green(f"✓ approved {args.approval_id} by {args.actor}"))
        return 0
    finally:
        getattr(store, "close", lambda: None)()


def cmd_reject(args: argparse.Namespace) -> int:
    (
        ApprovalState, ApprovalStore, ApprovalStoreError, *_,
    ) = _import_store()
    store = _open_store(args)
    try:
        try:
            record = store.get(args.approval_id)
        except Exception as exc:
            if "not found" in str(exc).lower():
                print(_red(f"approval not found: {args.approval_id}"), file=sys.stderr)
                return 2
            raise
        if record.state != ApprovalState.PROPOSED:
            print(
                _red(
                    f"cannot reject: state is {record.state.value}"
                ),
                file=sys.stderr,
            )
            return 3

        try:
            store.reject(
                args.approval_id,
                decided_by=args.actor,
                reason=args.reason,
            )
        except ApprovalStoreError as exc:
            print(_red(f"store error: {exc}"), file=sys.stderr)
            return 8

        print(_yellow(f"✗ rejected {args.approval_id} by {args.actor}"))
        return 0
    finally:
        getattr(store, "close", lambda: None)()


def cmd_cancel(args: argparse.Namespace) -> int:
    (
        ApprovalState, ApprovalStore, ApprovalStoreError, *_,
    ) = _import_store()
    store = _open_store(args)
    try:
        try:
            record = store.get(args.approval_id)
        except Exception as exc:
            if "not found" in str(exc).lower():
                print(_red(f"approval not found: {args.approval_id}"), file=sys.stderr)
                return 2
            raise
        if record.state != ApprovalState.PROPOSED:
            print(
                _red(
                    f"cannot cancel: state is {record.state.value}"
                ),
                file=sys.stderr,
            )
            return 3
        try:
            store.cancel(
                args.approval_id,
                cancelled_by=args.actor,
                reason=args.reason or "cancelled by operator",
            )
        except ApprovalStoreError as exc:
            print(_red(f"store error: {exc}"), file=sys.stderr)
            return 8
        print(_yellow(f"○ cancelled {args.approval_id}"))
        return 0
    finally:
        getattr(store, "close", lambda: None)()


def cmd_history(args: argparse.Namespace) -> int:
    _, ApprovalStore, *_ = _import_store()
    store = _open_store(args)
    try:
        # Phase 1 ApprovalStore에 list_recent() 없음 — 모든 상태를 조회해서 합산
        from src.execution.approval_store import ApprovalState
        all_records = []
        for state in ApprovalState:
            try:
                all_records.extend(store.list_by_state(state, limit=args.limit))
            except Exception:
                pass
        records = sorted(all_records, key=lambda r: r.created_at, reverse=True)[:args.limit]
        if not records:
            print("(no history)")
            return 0
        print(f"{_bold('Recent approvals')} (last {len(records)}):")
        print()
        for r in records:
            decided = (
                f" by {r.decided_by} at {r.decided_at.isoformat()}"
                if r.decided_at else ""
            )
            print(
                f"  {r.approval_id}  "
                f"{_format_state(r.state.value)}  "
                f"kind={r.action_kind if isinstance(r.action_kind, str) else r.action_kind.value}  "
                f"req={r.requested_by}{decided}"
            )
        return 0
    finally:
        getattr(store, "close", lambda: None)()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _open_store(args: argparse.Namespace):
    """Open the unified ApprovalStore at JCPR_APPROVAL_DB or --db."""
    _, ApprovalStore, *_ = _import_store()

    db_path = args.db or os.getenv("JCPR_APPROVAL_DB")
    if not db_path:
        print(
            _red(
                "JCPR_APPROVAL_DB env var or --db argument required "
                "(Phase 2: unified approvals.sqlite path)"
            ),
            file=sys.stderr,
        )
        sys.exit(1)

    return ApprovalStore(db_path=Path(db_path))


def _print_record_summary(record) -> None:
    print(_bold("Approval detail:"))
    print(f"  id          : {record.approval_id}")
    print(f"  kind        : {record.action_kind if isinstance(record.action_kind, str) else record.action_kind.value}")
    print(f"  requested_by: {record.requested_by}")
    print(f"  mode        : {record.mode}")
    print(f"  state       : {_format_state(record.state.value)}")
    print(f"  expires_at  : {record.expires_at}")
    print(f"  payload     :")
    for k, v in (getattr(record, "payload", None) or getattr(record, "action_payload", {})).items():
        print(f"    {k}: {v}")


# ---------------------------------------------------------------------------
# Argparse setup
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="approve_cli",
        description=(
            "JCPR unified approval CLI (Phase 2). "
            "Operates on the single approvals.sqlite at $JCPR_APPROVAL_DB."
        ),
    )
    p.add_argument(
        "--db",
        help="Path to approvals.sqlite (overrides JCPR_APPROVAL_DB)",
    )

    sub = p.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="List PROPOSED approvals")
    p_list.add_argument("--limit", type=int, default=50)
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="Show full detail of one approval")
    p_show.add_argument("approval_id")
    p_show.set_defaults(func=cmd_show)

    p_app = sub.add_parser("approve", help="Approve a PROPOSED action")
    p_app.add_argument("approval_id")
    p_app.add_argument("--actor", required=True, help="Operator actor id (must differ from requester)")
    p_app.add_argument("--comment", default=None)
    p_app.add_argument("--yes", action="store_true", help="Skip interactive confirmation")
    p_app.add_argument(
        "--yes-i-mean-live",
        action="store_true",
        help="Required for live-mode approvals (defense in depth)",
    )
    p_app.set_defaults(func=cmd_approve)

    p_rej = sub.add_parser("reject", help="Reject a PROPOSED action")
    p_rej.add_argument("approval_id")
    p_rej.add_argument("--actor", required=True)
    p_rej.add_argument("--reason", required=True)
    p_rej.set_defaults(func=cmd_reject)

    p_can = sub.add_parser("cancel", help="Cancel a still-PROPOSED action")
    p_can.add_argument("approval_id")
    p_can.add_argument("--actor", required=True)
    p_can.add_argument("--reason", default=None)
    p_can.set_defaults(func=cmd_cancel)

    p_hist = sub.add_parser("history", help="Show recent decisions")
    p_hist.add_argument("--limit", type=int, default=20)
    p_hist.set_defaults(func=cmd_history)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
