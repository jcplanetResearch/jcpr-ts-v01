"""src/signals/schema.py — Task 15 v0.2: 시그널 스키마 (Signal Schema).

전략(strategy)이 산출하는 표준 시그널 데이터 모델.

v0.2 변경 (Changes from v0.1)
-----------------------------
- SignalCategory 열거형 추가 (5개 값, 우선순위 보유)
- Signal.signal_category 필드 추가 (필수)
- Signal.priority() 헬퍼 추가 (러너 정렬용)
- SignalBatch.signals_sorted_by_priority() 추가
- __repr__ 에 category 노출
- MINIMUM_SIGNAL_CYCLE_SECONDS 상수 추가 (5초)

Concurrency assumption (MVP)
----------------------------
시그널 러너는 순차 처리(sequential). 동시성은 post-MVP 단계.

Frequency assumption (MVP)
--------------------------
최소 5초, 권고 1분 이상. 서브-초 시그널 미지원.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# ============================================================
# 1. 열거형 (Enums)
# ============================================================

class SignalAction(str, Enum):
    """전략이 산출할 수 있는 의사결정 종류."""
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    CLOSE = "CLOSE"


class SignalStrength(str, Enum):
    """신호 강도 — 사이징 정책에 영향을 주는 정성적(discrete) 등급."""
    WEAK = "WEAK"
    MEDIUM = "MEDIUM"
    STRONG = "STRONG"


class SignalCategory(str, Enum):
    """시그널 카테고리 — 우선순위 및 출력 #6 세분화에 사용 (v0.2 신규).

    우선순위 (낮을수록 먼저 처리):
    - STOP_LOSS, RISK_REDUCE : 1 (최고)
    - EXIT                    : 2
    - REBALANCE              : 3
    - ENTRY                  : 4 (최저)
    """
    STOP_LOSS = "STOP_LOSS"
    RISK_REDUCE = "RISK_REDUCE"
    EXIT = "EXIT"
    REBALANCE = "REBALANCE"
    ENTRY = "ENTRY"


_CATEGORY_PRIORITY: dict[SignalCategory, int] = {
    SignalCategory.STOP_LOSS: 1,
    SignalCategory.RISK_REDUCE: 1,
    SignalCategory.EXIT: 2,
    SignalCategory.REBALANCE: 3,
    SignalCategory.ENTRY: 4,
}


# ============================================================
# 2. 시크릿 패턴 검사
# ============================================================

_SECRET_PATTERNS = [
    re.compile(r"(?i)\bapp[_-]?key\b\s*[:=]"),
    re.compile(r"(?i)\bapp[_-]?secret\b\s*[:=]"),
    re.compile(r"(?i)\baccess[_-]?token\b\s*[:=]"),
    re.compile(r"(?i)\bsecret[_-]?key\b\s*[:=]"),
    re.compile(r"(?i)\bpassword\b\s*[:=]"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]{20,}"),
]

_SECRET_METADATA_KEYS = {
    "app_key", "appkey", "app-key",
    "app_secret", "appsecret", "app-secret",
    "access_token", "accesstoken", "access-token",
    "secret_key", "secretkey", "secret-key",
    "password", "passwd",
    "api_key", "apikey", "api-key",
    "private_key", "privatekey", "private-key",
}


def _contains_secret_pattern(text: str) -> bool:
    return any(pat.search(text) for pat in _SECRET_PATTERNS)


def _has_secret_metadata_key(metadata: dict[str, Any]) -> Optional[str]:
    for key in metadata.keys():
        if not isinstance(key, str):
            continue
        if key.lower().strip() in _SECRET_METADATA_KEYS:
            return key
    return None


# ============================================================
# 3. 상수
# ============================================================

_AS_OF_FUTURE_GRACE = timedelta(minutes=5)

# 최소 시그널 주기 — `<assumption>` 의 5초 제약. 러너에서 등록 시 검증 권고.
MINIMUM_SIGNAL_CYCLE_SECONDS = 5


# ============================================================
# 4. Signal 모델
# ============================================================

class Signal(BaseModel):
    """전략이 산출하는 단일 시그널 (immutable)."""
    model_config = {
        "frozen": True,
        "validate_assignment": True,
        "extra": "forbid",
    }

    # ---- 식별자
    signal_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    strategy_name: str = Field(..., min_length=1, max_length=64)
    strategy_version: str = Field(..., min_length=1, max_length=32)

    # ---- 대상 및 행동
    symbol: str = Field(..., min_length=1, max_length=16)
    action: SignalAction
    strength: SignalStrength
    signal_category: SignalCategory = Field(
        ...,
        description="v0.2 신규 필수. 우선순위 결정과 출력 #6 세분화.",
    )

    # ---- 시점
    created_at_utc: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    as_of_utc: datetime = Field(...)
    expires_at_utc: Optional[datetime] = Field(default=None)

    # ---- 가격·확신도
    reference_price: Decimal
    confidence: Optional[Decimal] = Field(default=None)

    # ---- 재현성·메타
    inputs_hash: Optional[str] = Field(default=None, max_length=128)
    metadata: dict[str, Any] = Field(default_factory=dict)
    notes: Optional[str] = Field(default=None, max_length=1000)

    # ============================================================
    # 검증
    # ============================================================

    @field_validator("created_at_utc", "as_of_utc")
    @classmethod
    def _ensure_required_utc_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("datetime must be tz-aware")
        return v.astimezone(timezone.utc)

    @field_validator("expires_at_utc")
    @classmethod
    def _ensure_optional_utc_aware(cls, v: Optional[datetime]) -> Optional[datetime]:
        if v is None:
            return v
        if v.tzinfo is None:
            raise ValueError("expires_at_utc must be tz-aware")
        return v.astimezone(timezone.utc)

    @field_validator("symbol")
    @classmethod
    def _normalize_symbol(cls, v: str) -> str:
        v = v.strip().upper()
        if not v:
            raise ValueError("symbol must not be empty")
        if not re.fullmatch(r"[A-Z0-9.\-]+", v):
            raise ValueError("symbol must contain only alphanumerics, '.', '-'")
        return v

    @field_validator("strategy_name", "strategy_version")
    @classmethod
    def _strip_and_check_nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be empty after strip")
        return v

    @field_validator("reference_price")
    @classmethod
    def _ensure_positive_price(cls, v: Decimal) -> Decimal:
        if not isinstance(v, Decimal):
            v = Decimal(str(v))
        if v <= 0:
            raise ValueError(f"reference_price must be positive, got {v}")
        return v

    @field_validator("confidence")
    @classmethod
    def _validate_confidence(cls, v: Optional[Decimal]) -> Optional[Decimal]:
        if v is None:
            return v
        if not isinstance(v, Decimal):
            v = Decimal(str(v))
        if not (Decimal("0") <= v <= Decimal("1")):
            raise ValueError(f"confidence must be in [0, 1], got {v}")
        return v

    @field_validator("inputs_hash")
    @classmethod
    def _validate_inputs_hash(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip().lower()
        if not v:
            raise ValueError("inputs_hash must not be empty if set")
        if not re.fullmatch(r"[A-Za-z0-9._\-]+", v):
            raise ValueError("inputs_hash must contain only alphanumerics, '.', '_', '-'")
        return v

    @field_validator("notes")
    @classmethod
    def _validate_notes_no_secrets(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if _contains_secret_pattern(v):
            raise ValueError("notes appears to contain a secret pattern; refused")
        return v

    @field_validator("metadata")
    @classmethod
    def _validate_metadata_no_secret_keys(cls, v: dict[str, Any]) -> dict[str, Any]:
        bad = _has_secret_metadata_key(v)
        if bad is not None:
            raise ValueError(f"metadata key {bad!r} matches a secret keyword; refused")
        return v

    @model_validator(mode="after")
    def _validate_temporal_consistency(self) -> "Signal":
        if self.as_of_utc > self.created_at_utc + _AS_OF_FUTURE_GRACE:
            raise ValueError(
                f"as_of_utc ({self.as_of_utc}) is more than "
                f"{_AS_OF_FUTURE_GRACE} after created_at_utc ({self.created_at_utc})"
            )
        if self.expires_at_utc is not None and self.expires_at_utc <= self.as_of_utc:
            raise ValueError(
                f"expires_at_utc ({self.expires_at_utc}) must be "
                f"after as_of_utc ({self.as_of_utc})"
            )
        return self

    # ============================================================
    # 헬퍼
    # ============================================================

    def is_expired(self, now_utc: Optional[datetime] = None) -> bool:
        if self.expires_at_utc is None:
            return False
        if now_utc is None:
            now_utc = datetime.now(timezone.utc)
        elif now_utc.tzinfo is None:
            raise ValueError("now_utc must be tz-aware")
        else:
            now_utc = now_utc.astimezone(timezone.utc)
        return now_utc >= self.expires_at_utc

    def is_actionable(self) -> bool:
        return self.action != SignalAction.HOLD

    def priority(self) -> int:
        """v0.2 신규. 시그널 우선순위 (1=최고 ~ 4=최저).

        러너(Task 16)는 본 값으로 정렬, 동일 우선순위 내에서는 as_of_utc 시각순.
        """
        return _CATEGORY_PRIORITY[self.signal_category]

    def __repr__(self) -> str:
        return (
            f"<Signal id={self.signal_id[:8]}... "
            f"strategy={self.strategy_name}@{self.strategy_version} "
            f"symbol={self.symbol} {self.action.value}/{self.strength.value} "
            f"cat={self.signal_category.value}>"
        )


# ============================================================
# 5. SignalBatch
# ============================================================

class SignalBatch(BaseModel):
    """한 번의 사이클에서 산출된 시그널 묶음."""
    model_config = {
        "frozen": True,
        "validate_assignment": True,
        "extra": "forbid",
    }

    batch_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    strategy_name: str = Field(..., min_length=1, max_length=64)
    strategy_version: str = Field(..., min_length=1, max_length=32)
    generated_at_utc: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    signals: tuple[Signal, ...] = Field(default_factory=tuple)
    universe_size: int = Field(default=0, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("generated_at_utc")
    @classmethod
    def _ensure_utc_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("generated_at_utc must be tz-aware")
        return v.astimezone(timezone.utc)

    @field_validator("strategy_name", "strategy_version")
    @classmethod
    def _strip_nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be empty after strip")
        return v

    @field_validator("metadata")
    @classmethod
    def _no_secret_keys(cls, v: dict[str, Any]) -> dict[str, Any]:
        bad = _has_secret_metadata_key(v)
        if bad is not None:
            raise ValueError(f"metadata key {bad!r} matches a secret keyword; refused")
        return v

    @model_validator(mode="after")
    def _validate_batch_consistency(self) -> "SignalBatch":
        for s in self.signals:
            if s.strategy_name != self.strategy_name:
                raise ValueError(
                    f"signal {s.signal_id[:8]} strategy_name {s.strategy_name!r} "
                    f"does not match batch {self.strategy_name!r}"
                )
            if s.strategy_version != self.strategy_version:
                raise ValueError(
                    f"signal {s.signal_id[:8]} strategy_version "
                    f"{s.strategy_version!r} does not match batch {self.strategy_version!r}"
                )
        if self.universe_size < len(self.signals):
            raise ValueError(
                f"universe_size ({self.universe_size}) must be >= "
                f"number of signals ({len(self.signals)})"
            )
        return self

    def actionable_signals(self) -> tuple[Signal, ...]:
        return tuple(s for s in self.signals if s.is_actionable())

    def hold_count(self) -> int:
        return sum(1 for s in self.signals if s.action == SignalAction.HOLD)

    def signals_sorted_by_priority(self) -> tuple[Signal, ...]:
        """v0.2 신규. 우선순위 + as_of_utc 순 정렬.

        동일 우선순위 내에서는 as_of_utc 시각순 (FCFS).
        러너(Task 16)가 활용.
        """
        return tuple(
            sorted(self.signals, key=lambda s: (s.priority(), s.as_of_utc))
        )

    def __repr__(self) -> str:
        n_actionable = len(self.actionable_signals())
        return (
            f"<SignalBatch id={self.batch_id[:8]}... "
            f"strategy={self.strategy_name}@{self.strategy_version} "
            f"n_signals={len(self.signals)} "
            f"actionable={n_actionable} "
            f"universe={self.universe_size}>"
        )


__all__ = [
    "MINIMUM_SIGNAL_CYCLE_SECONDS",
    "Signal",
    "SignalAction",
    "SignalBatch",
    "SignalCategory",
    "SignalStrength",
]
