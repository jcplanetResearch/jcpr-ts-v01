"""src/signals/_decision.py — Task 16: RunnerDecision (Stage 6 출력).

러너의 산출물 데이터 모델.
- RejectedSignal : 단일 거부 시그널 + 사유 + 메타데이터
- StageMetrics   : 단계별 처리 건수
- RunnerDecision : frozen 통합 결정 (Task 17 OrderIntent 입력)

Security
--------
metadata 는 시크릿 패턴 차단 (Task 15 schema 와 동일 규칙).
__repr__ 은 시크릿 미포함 안전한 표현만 산출.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.risk import RejectionReason
from src.signals._runner_state import RunnerStopReason
from src.signals.schema import Signal


_SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_\-]?key|secret|password|token|bearer)"),
    re.compile(r"[A-Za-z0-9]{32,}"),
]


def _check_metadata_secrets(metadata: dict[str, Any]) -> None:
    """metadata 시크릿 패턴 검사 — fail-closed."""
    for k, v in metadata.items():
        for p in _SECRET_PATTERNS:
            if p.search(str(k)):
                raise ValueError(f"metadata key '{k}' appears secret-like")
            if p.search(str(v)):
                raise ValueError(f"metadata value for key '{k}' appears secret-like")


# ============================================================
# 1. RejectedSignal
# ============================================================

class RejectedSignal(BaseModel):
    """거부된 시그널 + 사유 + 거부 컨텍스트."""
    model_config = ConfigDict(frozen=True)

    signal: Signal
    reason: RejectionReason
    stage: int = Field(ge=0, le=6)  # 0=preflight, 1~5=파이프라인, 6=emit
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _no_secrets(self) -> "RejectedSignal":
        _check_metadata_secrets(self.metadata)
        return self

    def __repr__(self) -> str:
        return (
            f"<RejectedSignal {self.signal.symbol} "
            f"{self.signal.action.value} reason={self.reason.value} "
            f"stage={self.stage}>"
        )


# ============================================================
# 2. StageMetrics
# ============================================================

class StageMetrics(BaseModel):
    """단계별 처리 지표.

    각 stage 의 in/out 건수 — 출력 #11 (exceptions) 및
    #9 (risk-limit usage) 의 계산 입력.
    """
    model_config = ConfigDict(frozen=True)

    stage_1_filter_in: int = Field(default=0, ge=0)
    stage_1_filter_out: int = Field(default=0, ge=0)
    stage_2_dedup_in: int = Field(default=0, ge=0)
    stage_2_dedup_out: int = Field(default=0, ge=0)
    stage_3_conflict_in: int = Field(default=0, ge=0)
    stage_3_conflict_out: int = Field(default=0, ge=0)
    stage_4_sort_in: int = Field(default=0, ge=0)
    stage_4_sort_out: int = Field(default=0, ge=0)
    stage_5_resolve_in: int = Field(default=0, ge=0)
    stage_5_resolve_out: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _consistency(self) -> "StageMetrics":
        """각 stage out <= in 검증."""
        pairs = [
            ("stage_1_filter", self.stage_1_filter_in, self.stage_1_filter_out),
            ("stage_2_dedup", self.stage_2_dedup_in, self.stage_2_dedup_out),
            ("stage_3_conflict", self.stage_3_conflict_in, self.stage_3_conflict_out),
            ("stage_4_sort", self.stage_4_sort_in, self.stage_4_sort_out),
            ("stage_5_resolve", self.stage_5_resolve_in, self.stage_5_resolve_out),
        ]
        for name, in_n, out_n in pairs:
            if out_n > in_n:
                raise ValueError(f"{name}: out ({out_n}) > in ({in_n})")
        return self

    def passthrough_rate(self) -> float:
        """Stage 1 입력 대비 Stage 5 출력 비율.

        0건 처리 시 0.0 반환.
        """
        if self.stage_1_filter_in == 0:
            return 0.0
        return self.stage_5_resolve_out / self.stage_1_filter_in


# ============================================================
# 3. RunnerDecision
# ============================================================

class RunnerDecision(BaseModel):
    """시그널 러너 결정 — Task 17 OrderIntent 변환의 입력."""
    model_config = ConfigDict(frozen=True)

    decision_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    cycle_id: str
    runner_version: str = "v1.0.0"
    as_of_utc: datetime  # tz-aware UTC

    input_batch_id: str
    input_strategy_name: str
    input_strategy_version: str

    accepted_signals: tuple[Signal, ...] = Field(default_factory=tuple)
    rejected_signals: tuple[RejectedSignal, ...] = Field(default_factory=tuple)

    stage_metrics: StageMetrics = Field(default_factory=StageMetrics)

    # Stage 0 결과
    stop_engaged: bool = False
    stop_reason: Optional[RunnerStopReason] = None
    cadence_violation: bool = False
    cadence_elapsed_seconds: Optional[float] = None
    cadence_next_allowed_at: Optional[datetime] = None

    # Stage 5 결과
    available_capital_at_start: Optional[Decimal] = None
    capital_consumed_estimate: Optional[Decimal] = None

    @field_validator("as_of_utc", "cadence_next_allowed_at")
    @classmethod
    def _tz_aware(cls, v: Optional[datetime]) -> Optional[datetime]:
        if v is None:
            return None
        if v.tzinfo is None:
            raise ValueError("datetime must be tz-aware")
        return v.astimezone(timezone.utc)

    @field_validator("cycle_id", "input_batch_id", "input_strategy_name",
                     "input_strategy_version", "runner_version")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("must not be empty")
        return v.strip()

    @model_validator(mode="after")
    def _consistency(self) -> "RunnerDecision":
        # stop 활성 시 stop_reason 필수
        if self.stop_engaged and self.stop_reason is None:
            raise ValueError("stop_reason required when stop_engaged=True")
        if not self.stop_engaged and self.stop_reason is not None:
            raise ValueError("stop_reason must be None when stop_engaged=False")

        # cadence 위반 시 elapsed 또는 next_allowed_at 필요
        if self.cadence_violation and self.cadence_next_allowed_at is None:
            raise ValueError("cadence_next_allowed_at required when cadence_violation=True")

        # stop/cadence 활성 시 accepted 비어야 함
        if (self.stop_engaged or self.cadence_violation) and len(self.accepted_signals) > 0:
            raise ValueError("accepted_signals must be empty when stop or cadence violated")

        # 자본 일관성
        if self.capital_consumed_estimate is not None:
            if self.available_capital_at_start is None:
                raise ValueError("available_capital_at_start required when capital_consumed set")
            if self.capital_consumed_estimate < Decimal("0"):
                raise ValueError("capital_consumed_estimate must be >= 0")

        return self

    def is_actionable(self) -> bool:
        """Task 17 OrderIntent 변환 진행 가능 여부."""
        return (
            not self.stop_engaged
            and not self.cadence_violation
            and len(self.accepted_signals) > 0
        )

    def total_input_signals(self) -> int:
        """입력 단계 (Stage 1 in)."""
        return self.stage_metrics.stage_1_filter_in

    def __repr__(self) -> str:
        flags = []
        if self.stop_engaged:
            flags.append(f"STOP={self.stop_reason.value if self.stop_reason else '?'}")
        if self.cadence_violation:
            flags.append("CADENCE_VIOL")
        flag_str = " " + " ".join(flags) if flags else ""
        return (
            f"<RunnerDecision id={self.decision_id[:8]}... "
            f"cycle={self.cycle_id} "
            f"strategy={self.input_strategy_name}@{self.input_strategy_version} "
            f"accepted={len(self.accepted_signals)} "
            f"rejected={len(self.rejected_signals)}{flag_str}>"
        )


__all__ = [
    "RejectedSignal",
    "RunnerDecision",
    "StageMetrics",
]
