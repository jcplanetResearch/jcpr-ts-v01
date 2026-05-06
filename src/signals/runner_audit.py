"""
사이클 감사 로그 (Cycle Audit Log)
====================================

JCPR Trading System - jcpr-ts-v01
Task 16 v0.3 보조 모듈

SignalRunner의 사이클 결과를 JSONL로 로컬 기록.
(Records SignalRunner cycle results as local JSONL.)

원칙 (Principles):
- 비밀 데이터 미포함 (no secrets)
- UTC tz-aware datetime
- append-only
- Zone D (Local Only)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _json_default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, Enum):
        return o.value
    raise TypeError(f"JSON 직렬화 불가: {type(o)}")


class CycleAuditLog:
    """사이클 단위 JSONL audit log."""

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def write(self, record: dict[str, Any]) -> None:
        try:
            line = json.dumps(record, default=_json_default, ensure_ascii=False) + "\n"
            with self._path.open("a", encoding="utf-8") as f:
                f.write(line)
        except OSError as e:
            logger.error("Cycle audit log 기록 실패: %s", e)


def build_cycle_record(
    *,
    cycle_id: str,
    started_at_utc: datetime,
    completed_at_utc: datetime,
    watchlist: list[str],
    stats: dict[str, Any],
    aborted: bool,
    abort_reason: str | None,
    per_symbol_summary: list[dict[str, Any]],
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    audit log용 사이클 기록 빌드.
    (Build cycle record for audit log — no secrets included.)
    """
    return {
        "cycle_id": cycle_id,
        "started_at_utc": started_at_utc.astimezone(timezone.utc).isoformat(),
        "completed_at_utc": completed_at_utc.astimezone(timezone.utc).isoformat(),
        "elapsed_sec": (completed_at_utc - started_at_utc).total_seconds(),
        "watchlist_size": len(watchlist),
        "watchlist": watchlist,
        "stats": stats,
        "aborted": aborted,
        "abort_reason": abort_reason,
        "per_symbol_summary": per_symbol_summary,
        "metadata": metadata or {},
    }
