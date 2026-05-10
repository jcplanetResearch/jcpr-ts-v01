"""Reconciliation jsonl reader (A3-1, separation of concerns pattern).

Mirrors src/dashboard/_audit_reader.py — read-only, malformed-line
graceful, max_lines cap, 0600 enforcement (layer 17).

Reads ReconciliationReport.to_dict() rows produced by an external
reconciler process (e.g., scripts/run_reconciler.py). The dashboard
NEVER holds KIS credentials directly; the reconciler runs separately
with its own credential scope, writes results here, and the dashboard
reads the latest row.

Schema (from Task 28 reconciliation.py — ReconciliationReport.to_dict):
    captured_at_utc:               iso str
    severity:                      "ok" | "minor" | "major"
    all_matched:                   bool
    broker_position_count:         int
    ledger_position_count:         int
    match_count:                   int
    mismatch_count:                int
    matches:                       list[str]   # symbols that matched
    mismatches:                    list[dict]  # PositionMismatch.to_dict
    by_type:                       dict[str, int]
    broker_cash_krw:               str (Decimal serialized)
    broker_total_evaluation_krw:   str (Decimal serialized)
    tolerance:                     dict
"""
from __future__ import annotations

import json
import logging
import os
import stat
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


__all__ = [
    "ReconciliationReadError",
    "ReconciliationReader",
]


logger = logging.getLogger(__name__)


# Hard ceiling on lines pulled into memory in a single read. Reconciliation
# is typically run a few times per session, so 1000 rows covers months of
# audit history while keeping memory bounded.
_DEFAULT_MAX_LINES: int = 1000


class ReconciliationReadError(Exception):
    """Raised on permission / IO failures that block reading at all.

    Mirrors AuditReadError semantics — malformed individual lines are
    NOT raised, they log a warning and are skipped. This exception
    signals the file itself is unreadable (permissions, disk error).
    """


class ReconciliationReader:
    """Read-only JSONL reader for reconciliation audit log.

    Args:
        path: path to the JSONL file. Absent files are tolerated by
            the read methods (return empty / None).
        max_lines: ceiling on lines read in a single call. Default 1000.
        verify_permissions: when True (default), opening the file checks
            for mode 0600 on POSIX. False is intended only for tests.

    Thread-safety:
        Each method opens and closes the file independently. Concurrent
        reads are safe; concurrent writes by the external reconciler are
        also safe because JSONL is line-atomic on POSIX append.
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
            raise ReconciliationReadError(
                f"reconciliation audit {self._path} has mode {oct(actual)}, "
                f"required 0o600. Fix: chmod 600 {self._path}"
            )

    def _read_all_lines(self) -> list[str]:
        """Read all lines (capped at max_lines from end) without parsing.

        Returns the last `max_lines` non-blank lines from the file. Used
        as the staging input for `_parse` — read once, parse multiple
        times if the same instance is queried for both `latest()` and
        `history()`.
        """
        self._check_permissions()
        if not self._path.exists():
            return []
        try:
            with self._path.open("r", encoding="utf-8") as f:
                # Bounded ring buffer — never load more than max_lines
                kept: deque[str] = deque(maxlen=self._max_lines)
                for raw in f:
                    raw = raw.strip()
                    if raw:
                        kept.append(raw)
                return list(kept)
        except OSError as exc:
            raise ReconciliationReadError(
                f"failed to read {self._path}: {exc}"
            ) from exc

    def _parse(self, lines: list[str]) -> list[dict[str, Any]]:
        """Parse JSONL lines, skipping malformed ones with a warning.

        The reconciler writes one row per reconciliation run; partial
        writes during crash are possible. We tolerate them silently
        (log warning, skip the line) so the dashboard always shows
        whatever valid data exists.
        """
        out: list[dict[str, Any]] = []
        skipped = 0
        for line in lines:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                skipped += 1
                logger.warning(
                    "skipping malformed reconciliation line in %s: %s",
                    self._path.name, exc,
                )
                continue
            if isinstance(obj, dict):
                out.append(obj)
            else:
                skipped += 1
                logger.warning(
                    "skipping non-dict reconciliation record in %s (got %s)",
                    self._path.name, type(obj).__name__,
                )
        if skipped:
            logger.info(
                "reconciliation reader skipped %d malformed lines in %s",
                skipped, self._path.name,
            )
        return out

    # ---- public API ----------------------------------------------------

    def latest(self) -> Optional[dict[str, Any]]:
        """Return the most-recent reconciliation row, or None if none.

        "Most recent" is the LAST line in the file (jsonl is append-only).
        For chronological correctness this assumes the reconciler appends
        in real time without re-ordering — which Task 28 does
        (`captured_at_utc=datetime.now(timezone.utc)` at write time).

        Returns:
            The parsed dict from the last valid line, or None when the
            file is absent / empty / contains only malformed lines.

        Raises:
            ReconciliationReadError on permission / IO failure.
        """
        records = self._parse(self._read_all_lines())
        if not records:
            return None
        return records[-1]

    def history(self, n: int = 10) -> list[dict[str, Any]]:
        """Return the last `n` reconciliation rows, oldest first.

        Args:
            n: number of rows to return; capped at max_lines.

        Returns:
            List of parsed dicts (empty if file absent / empty).

        Raises:
            ReconciliationReadError on permission / IO failure.
        """
        if n <= 0:
            return []
        records = self._parse(self._read_all_lines())
        cap = min(n, self._max_lines)
        return records[-cap:]

    def latest_severity(self) -> Optional[str]:
        """Convenience: severity of the most recent run.

        Returns:
            "ok" / "minor" / "major" / None (none = no data yet).

        Raises:
            ReconciliationReadError on permission / IO failure.
        """
        latest = self.latest()
        if latest is None:
            return None
        sev = latest.get("severity")
        return str(sev) if sev else None
