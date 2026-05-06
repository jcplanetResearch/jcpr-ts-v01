"""
사이징 감사 로그 (Sizing Audit Log)
====================================

JCPR Trading System - jcpr-ts-v01
Task 18 v0.2 보조 모듈

사이징 계산 과정 전체를 구조화된 형태로 로컬 DB/파일에 기록.
(Records full sizing calculation as structured audit log to local DB/file.)

원칙 (Principles):
- 비밀/키 데이터 절대 기록 안 함 (never log secrets/keys)
- 모든 시각은 UTC tz-aware 저장, 표시 시 KST 변환 (store UTC, display KST)
- 로컬 전용 (local-only)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)


def _decimal_default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, datetime):
        return o.isoformat()
    raise TypeError(f"JSON 직렬화 불가 (not JSON serializable): {type(o)}")


@dataclass
class SizingDecision:
    """
    사이징 결정 한 건의 감사 기록.
    (One sizing decision audit record.)
    """
    decision_id: str
    timestamp_utc: datetime
    strategy_id: str
    symbol: str
    side: str
    sizing_method: str            # "fixed_pct" | "atr" | "fixed_risk"
    inputs: dict[str, Any]        # 입력 파라미터 (no secrets)
    intermediate: dict[str, Any]  # 중간 계산
    raw_quantity: int             # 정렬 전 수량
    final_quantity: int           # 정렬 후 (호가/거래단위 반영) 수량
    raw_price: Decimal | None
    aligned_price: Decimal | None
    estimated_cost: Decimal       # 예상 명목 금액 (KRW)
    decision: str                 # "accept" | "reject"
    reject_reason: str | None = None
    notes: list[str] = field(default_factory=list)

    @classmethod
    def new(cls, **kwargs: Any) -> "SizingDecision":
        return cls(
            decision_id=str(uuid4()),
            timestamp_utc=datetime.now(timezone.utc),
            **kwargs,
        )

    def to_json(self) -> str:
        d = asdict(self)
        return json.dumps(d, default=_decimal_default, ensure_ascii=False)


class SizingAuditLogger:
    """
    JSONL 형식으로 로컬에 추가 기록 (append-only JSONL to local file).
    프로덕션에서는 SQLite/PostgreSQL로 교체 가능.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, decision: SizingDecision) -> None:
        try:
            line = decision.to_json() + "\n"
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line)
        except OSError as e:
            # 로깅 실패는 치명적이지 않으나 경고 (failure logged but non-fatal)
            logger.error("사이징 감사 로그 기록 실패 (sizing audit write failed): %s", e)
