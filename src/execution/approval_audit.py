"""
승인 감사 로그 (Approval Audit Log)
====================================

JCPR Trading System - jcpr-ts-v01
Task 40 v0.1

ApprovalProvider 결정을 JSONL로 영속화.
(Persists ApprovalProvider decisions as JSONL.)

Zone D (Local Only) — `.gitignore` 처리됨.

원칙 (Principles):
- 비밀 데이터 미포함 (계좌번호, 토큰 등)
- UTC tz-aware datetime
- append-only
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from .approval import ApprovalDecision, ApprovalRequest

logger = logging.getLogger(__name__)


def _json_default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, Enum):
        return o.value
    raise TypeError(f"JSON 직렬화 불가: {type(o)}")


class ApprovalAuditLog:
    """JSONL 기반 승인 audit log."""

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def write(
        self,
        request: ApprovalRequest,
        decision: ApprovalDecision,
        *,
        response_time_sec: Optional[float] = None,
        provider_chain: Optional[list[str]] = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> None:
        """승인 결정 1건 기록."""
        record = {
            "execution_id": request.execution_id,
            "signal_id": request.signal_id,
            "decided_at_utc": decision.decided_at_utc.astimezone(timezone.utc).isoformat(),
            "requested_at_utc": request.requested_at_utc.astimezone(timezone.utc).isoformat(),
            "approved": decision.approved,
            "approver": decision.approver,
            "reason": decision.reason,
            "symbol": request.symbol,
            "side": request.side,
            "quantity": request.quantity,
            "price_krw": str(request.price),
            "estimated_cost_krw": str(request.estimated_cost_krw),
            "is_live_env": request.is_live_env,
            "is_dry_run": request.is_dry_run,
            "response_time_sec": response_time_sec,
            "provider_chain": provider_chain or [],
        }
        if extra:
            # 비밀 키 안전 검사 — 명시적 거부 키워드
            safe_extra = {
                k: v for k, v in extra.items()
                if not any(blocked in k.lower() for blocked in
                           ["secret", "token", "password", "app_key", "account_no"])
            }
            record["extra"] = safe_extra

        try:
            line = json.dumps(record, default=_json_default, ensure_ascii=False) + "\n"
            with self._path.open("a", encoding="utf-8") as f:
                f.write(line)
        except OSError as e:
            logger.error("Approval audit 기록 실패: %s", e)
