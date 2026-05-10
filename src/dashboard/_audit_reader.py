"""JSONL audit log readers for the dashboard.

Two audit logs are read here (decisions D1, D2):

    risk_decisions.jsonl  (D1) — RiskGate.evaluate() outputs per order.
                                 Used for output item #9 (risk-limit usage).
    executions.jsonl      (D2) — broker submission/fill records.
                                 Used as input to SlippageAnalyzer
                                 (output item #5).

Both files are append-only JSONL written by upstream tasks (Tasks 19/20
for risk decisions, Tasks 21/24 for executions). The dashboard treats
them as read-only and resilient: malformed lines are skipped with a
warning rather than aborting the whole read.

The reader caps the number of lines pulled into memory (max_lines)
even when the operator requests `tail(big_n)`. This protects against
runaway audit growth crashing the dashboard process.
"""
from __future__ import annotations

import json
import logging
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional


__all__ = [
    "AuditReadError",
    "AuditLogReader",
    "RISK_DECISION_FIELDS",
]


logger = logging.getLogger(__name__)


# Expected field names in risk_decisions.jsonl. Used by aggregate_risk_decisions
# to count outcomes per check. The actual upstream writer is Task 20
# (risk rejection reporting); the format below mirrors GateDecision.to_dict().
RISK_DECISION_FIELDS: tuple[str, ...] = (
    "decided_at",
    "client_order_id",
    "approved",
    "rejection_reason",
    "rejection_detail",
    "failed_check",
    "check_results",
)


# Hard ceiling on lines pulled into memory in a single read. Protects against
# an audit file that has grown to gigabytes from crashing the dashboard.
_DEFAULT_MAX_LINES: int = 10000


class AuditReadError(Exception):
    """Raised on permission / IO failures that block reading at all.

    Malformed individual lines are NOT raised — they log a warning and
    are skipped. AuditReadError signals the file itself is unreadable.
    """


class AuditLogReader:
    """Read-only JSONL audit reader with permission verification.

    Args:
        path: path to the JSONL file. Absent files are tolerated by
            the read methods (return empty), but constructor still
            stores the path for later existence-checked operations.
        max_lines: ceiling on lines read in a single call. Default 10000.
        verify_permissions: when True (default), opening the file checks
            for mode 0600 on POSIX. False is intended only for tests.

    Thread-safety:
        Each method opens and closes the file independently. Concurrent
        reads are safe; concurrent writes by upstream are also safe
        because JSONL is line-atomic on POSIX append.
    """

    def __init__(
        self,
        path: Path,
        *,
        max_lines: int = _DEFAULT_MAX_LINES,
        verify_permissions: bool = True,
    ) -> None:
        if max_lines <= 0:
            raise ValueError(f"max_lines must be positive, got {max_lines}")
        self._path = Path(path)
        self._max_lines = max_lines
        self._verify_perms = verify_permissions

    @property
    def path(self) -> Path:
        return self._path

    @property
    def exists(self) -> bool:
        return self._path.exists()

    def _check_permissions(self) -> None:
        """Enforce 0600 on POSIX. Skips if file absent or non-POSIX."""
        if not self._verify_perms:
            return
        if os.name != "posix":
            return
        if not self._path.exists():
            return
        actual = stat.S_IMODE(self._path.stat().st_mode)
        if actual != 0o600:
            raise AuditReadError(
                f"audit file {self._path} has mode {oct(actual)}, "
                f"required 0o600. Fix: chmod 600 {self._path}"
            )

    def _iter_lines(self) -> Iterable[str]:
        """Yield raw lines from the file. Empty iterator if absent."""
        self._check_permissions()
        if not self._path.exists():
            return
        try:
            with self._path.open("r", encoding="utf-8") as f:
                for raw in f:
                    raw = raw.strip()
                    if raw:
                        yield raw
        except OSError as exc:
            raise AuditReadError(
                f"failed to read audit file {self._path}: {exc}"
            ) from exc

    def _parse_lines(self, lines: Iterable[str]) -> list[dict]:
        """Parse JSONL lines, skipping malformed ones with a warning.

        Malformed lines are logged at WARNING level and skipped. The
        returned list contains only successfully parsed records.
        """
        out: list[dict] = []
        skipped = 0
        for line in lines:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                skipped += 1
                logger.warning(
                    "skipping malformed audit line in %s: %s",
                    self._path.name, exc,
                )
                continue
            if isinstance(obj, dict):
                out.append(obj)
            else:
                skipped += 1
                logger.warning(
                    "skipping non-dict audit record in %s (got %s)",
                    self._path.name, type(obj).__name__,
                )
        if skipped:
            logger.info(
                "audit reader skipped %d malformed lines in %s",
                skipped, self._path.name,
            )
        return out

    # ---- public API ----------------------------------------------------

    def tail(self, n: int = 100) -> list[dict]:
        """Return the last `n` records, oldest first.

        Args:
            n: number of records to return; capped at max_lines.

        Returns:
            List of parsed dicts (empty if file absent).

        Raises:
            AuditReadError on permission/IO failure.
        """
        if n <= 0:
            return []
        cap = min(n, self._max_lines)

        # Two-pass to avoid loading the whole file into memory: first
        # collect all lines into a bounded deque-style list keyed off
        # the cap, then parse only what we kept.
        from collections import deque
        keep: deque[str] = deque(maxlen=cap)
        for line in self._iter_lines():
            keep.append(line)
        return self._parse_lines(list(keep))

    def since(self, since_utc: datetime) -> list[dict]:
        """Return records whose 'decided_at' (or 'captured_at_utc') is >= since_utc.

        Looks for ISO timestamps in two canonical fields:
            decided_at        (RiskGate)
            captured_at_utc   (Reconciler / SlippageRecord)

        Records missing both timestamps are SKIPPED (not included).

        Args:
            since_utc: tz-aware UTC datetime cutoff (inclusive).

        Returns:
            List of records ordered as written (chronological if writer
            appended in real time).

        Raises:
            ValueError if since_utc is naive.
            AuditReadError on permission/IO failure.
        """
        if since_utc.tzinfo is None:
            raise ValueError("since_utc must be tz-aware")

        records = self._parse_lines(self._iter_lines())
        cutoff = since_utc.astimezone(timezone.utc)
        out: list[dict] = []

        for rec in records:
            ts_str = rec.get("decided_at") or rec.get("captured_at_utc")
            if not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
            except ValueError:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= cutoff:
                out.append(rec)
            # Cap output to max_lines as a final guard.
            if len(out) >= self._max_lines:
                logger.warning(
                    "since() in %s hit max_lines cap %d; truncating",
                    self._path.name, self._max_lines,
                )
                break
        return out

    def aggregate_risk_decisions(
        self,
        *,
        since_utc: Optional[datetime] = None,
    ) -> dict:
        """Aggregate RiskGate decisions for output item #9.

        Counts:
            - total decisions
            - approved / rejected counts
            - rejection_reason histogram
            - failed_check histogram (which of the 9 checks fired)
            - per-check pass/fail counts (from check_results sub-list)

        Args:
            since_utc: optional cutoff; if None, scans the whole file
                (capped by max_lines on read).

        Returns:
            dict with keys: total, approved, rejected, by_reason,
            by_failed_check, by_check_outcome.

        Raises:
            AuditReadError on permission/IO failure.
        """
        if since_utc is not None:
            records = self.since(since_utc)
        else:
            records = self.tail(self._max_lines)

        total = 0
        approved = 0
        rejected = 0
        by_reason: dict[str, int] = {}
        by_failed_check: dict[str, int] = {}
        by_check_outcome: dict[str, dict[str, int]] = {}

        for rec in records:
            total += 1
            is_approved = bool(rec.get("approved", False))
            if is_approved:
                approved += 1
            else:
                rejected += 1
                reason = rec.get("rejection_reason") or "unknown"
                by_reason[str(reason)] = by_reason.get(str(reason), 0) + 1
                failed = rec.get("failed_check") or "unknown"
                by_failed_check[str(failed)] = by_failed_check.get(str(failed), 0) + 1

            # Per-check outcomes from the embedded check_results array
            check_results = rec.get("check_results") or []
            if isinstance(check_results, list):
                for cr in check_results:
                    if not isinstance(cr, dict):
                        continue
                    name = str(cr.get("check_name", "unknown"))
                    passed = bool(cr.get("passed", False))
                    bucket = by_check_outcome.setdefault(
                        name, {"passed": 0, "failed": 0},
                    )
                    bucket["passed" if passed else "failed"] += 1

        return {
            "total": total,
            "approved": approved,
            "rejected": rejected,
            "by_reason": by_reason,
            "by_failed_check": by_failed_check,
            "by_check_outcome": by_check_outcome,
        }
