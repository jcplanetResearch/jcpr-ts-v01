"""src/signals/schema.py — Task 15: 시그널 스키마 (Signal Schema).

전략(strategy)이 산출하는 표준 시그널 데이터 모델.
모든 전략은 본 스키마를 따르는 Signal 또는 SignalBatch 를 산출한다.

본 모듈은 데이터 표준만 정의한다. 전략 로직은 Task 14, 러너는 Task 16,
OrderIntent 변환은 Task 17 + Task 18 의 책임이다.

Design separation
-----------------
- Signal       : 전략의 의견 (strategy's opinion) — 무엇을 살지/팔지, 얼마나 확신하는지
- OrderIntent  : 실행 의도 (execution intent) — 정확히 몇 주를 어느 가격에

Signal → OrderIntent 변환은 Task 16 (러너) + Task 18 (sizer) 책임.

Long-only assumption
--------------------
KRX 모의투자/일반 운영은 long-only 가정. SELL 은 보유 포지션 청산을 의미.
공매도(short-selling) 지원 시 SHORT_OPEN, SHORT_CLOSE 추가 예정.

Security
--------
- notes / metadata 에 우발적 시크릿 누출 방지 검사
- __repr__ 에는 식별자·핵심 행동만 노출
- frozen=True 로 불변
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
    """전략이 산출할 수 있는 의사결정 종류.

    - BUY   : 매수 진입 신호
    - SELL  : 매도 진입 신호 (long-only 시스템에서는 보유 포지션 종료)
    - HOLD  : 신호 없음, 현 상태 유지 (출력 #6 통계에 포함)
    - CLOSE : 기존 포지션 청산 (방향 무관)
    """
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    CLOSE = "CLOSE"


class SignalStrength(str, Enum):
    """신호 강도 — 사이징 정책에 영향을 주는 정성적(discrete) 등급.

    참고(advisory): capacity 한도를 우회할 수 없음.
    """
    WEAK = "WEAK"
    MEDIUM = "MEDIUM"
    STRONG = "STRONG"


# ============================================================
# 2. 시크릿 패턴 검사 (Secret Pattern Check) — Task 17 과 동일
# ============================================================

_SECRET_PATTERNS = [
    re.compile(r"(?i)\bapp[_-]?key\b\s*[:=]"),
    re.compile(r"(?i)\bapp[_-]?secret\b\s*[:=]"),
    re.compile(r"(?i)\baccess[_-]?token\b\s*[:=]"),
    re.compile(r"(?i)\bsecret[_-]?key\b\s*[:=]"),
    re.compile(r"(?i)\bpassword\b\s*[:=]"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]{20,}"),
]

# metadata 키 자체에 들어가면 안 되는 시크릿 키워드
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
    """텍스트에 시크릿 패턴이 포함되어 있는지 검사."""
    return any(pat.search(text) for pat in _SECRET_PATTERNS)


def _has_secret_metadata_key(metadata: dict[str, Any]) -> Optional[str]:
    """metadata 키에 시크릿 키워드가 있으면 그 키를 반환, 없으면 None."""
    for key in metadata.keys():
        if not isinstance(key, str):
            continue
        if key.lower().strip() in _SECRET_METADATA_KEYS:
            return key
    return None


# ============================================================
# 3. 상수 (Constants)
# ============================================================

# as_of_utc 가 created_at_utc 보다 미래여도 허용되는 작은 여유 (clock skew)
_AS_OF_FUTURE_GRACE = timedelta(minutes=5)


# ============================================================
# 4. Signal 모델 (Signal Model)
# ============================================================

class Signal(BaseModel):
    """전략이 산출하는 단일 시그널.

    한 번 생성되면 변경 불가(immutable). 만료(expires_at_utc) 되거나
    HOLD 인 경우 러너(Task 16)가 차단한다.
    """
    model_config = {
        "frozen": True,
        "validate_assignment": True,
        "extra": "forbid",
    }

    # ---- 4.1 식별자 (Identifiers)
    signal_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="시그널 고유 식별자 (UUID)",
    )
    strategy_name: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="출처 전략 이름 (출력 #6 strategy attribution 의 키)",
    )
    strategy_version: str = Field(
        ...,
        min_length=1,
        max_length=32,
        description='전략 버전 (예: "momentum_v1.0")',
    )

    # ---- 4.2 대상 및 행동 (Target & Action)
    symbol: str = Field(
        ...,
        min_length=1,
        max_length=16,
        description="대상 종목 코드 (KRX 6자리 등)",
    )
    action: SignalAction = Field(..., description="BUY/SELL/HOLD/CLOSE")
    strength: SignalStrength = Field(..., description="WEAK/MEDIUM/STRONG")

    # ---- 4.3 시점 (Timing)
    created_at_utc: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="시그널 인스턴스 생성 시각 (UTC tz-aware)",
    )
    as_of_utc: datetime = Field(
        ...,
        description=(
            "시그널의 기준 시각 (예: 5분봉 종료 시각). "
            "created_at_utc 보다 5분 이상 미래일 수 없음."
        ),
    )
    expires_at_utc: Optional[datetime] = Field(
        default=None,
        description=(
            "시그널 유효 만료 시각. None 이면 즉시 사용 권장. "
            "as_of_utc 보다 미래여야 함."
        ),
    )

    # ---- 4.4 가격 및 확신도 (Price & Confidence)
    reference_price: Decimal = Field(
        ...,
        description="시그널 산출 시점의 가격 — sizing 입력으로 사용 가능",
    )
    confidence: Optional[Decimal] = Field(
        default=None,
        description=(
            "정량적 확신도 (0.0 ~ 1.0). 있으면 sizing 가중치로 활용 가능. "
            "단, capacity 한도를 우회할 수 없음."
        ),
    )

    # ---- 4.5 재현성 및 메타 (Reproducibility & Meta)
    inputs_hash: Optional[str] = Field(
        default=None,
        max_length=128,
        description="입력 데이터 해시 (재현성·감사 목적). 16진수 문자열 권장.",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="전략별 자유 메타. 시크릿 키워드 포함 시 거부.",
    )
    notes: Optional[str] = Field(
        default=None,
        max_length=1000,
        description="운영자 메모. 시크릿 패턴 포함 시 거부.",
    )

    # ============================================================
    # 5. 검증 (Validators)
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
        # 영숫자 + '.', '-' 만 허용 (KRX 6자리 + ETF 등)
        if not re.fullmatch(r"[A-Z0-9.\-]+", v):
            raise ValueError(
                "symbol must contain only alphanumerics, '.', '-'"
            )
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
        # 16진수 형식 권장 — 다른 형식도 허용하되 화이트스페이스 차단
        if not re.fullmatch(r"[A-Za-z0-9._\-]+", v):
            raise ValueError(
                "inputs_hash must contain only alphanumerics, '.', '_', '-'"
            )
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
    def _validate_metadata_no_secret_keys(
        cls, v: dict[str, Any]
    ) -> dict[str, Any]:
        bad = _has_secret_metadata_key(v)
        if bad is not None:
            raise ValueError(
                f"metadata key {bad!r} matches a secret keyword; refused"
            )
        return v

    @model_validator(mode="after")
    def _validate_temporal_consistency(self) -> "Signal":
        # as_of_utc 가 created_at_utc 보다 5분 이상 미래이면 거부
        if self.as_of_utc > self.created_at_utc + _AS_OF_FUTURE_GRACE:
            raise ValueError(
                f"as_of_utc ({self.as_of_utc}) is more than "
                f"{_AS_OF_FUTURE_GRACE} after created_at_utc ({self.created_at_utc})"
            )
        # expires_at_utc 가 as_of_utc 이하이면 거부
        if self.expires_at_utc is not None and self.expires_at_utc <= self.as_of_utc:
            raise ValueError(
                f"expires_at_utc ({self.expires_at_utc}) must be "
                f"after as_of_utc ({self.as_of_utc})"
            )
        return self

    # ============================================================
    # 6. 헬퍼 (Helpers)
    # ============================================================

    def is_expired(self, now_utc: Optional[datetime] = None) -> bool:
        """현 시각 기준 시그널 만료 여부.

        expires_at_utc 가 None 이면 만료 개념 없음 → False.
        """
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
        """OrderIntent 로 변환 가능한 행동인지 (HOLD 제외).

        러너(Task 16)는 HOLD 시그널을 OrderIntent 변환 단계에서 차단.
        """
        return self.action != SignalAction.HOLD

    # ============================================================
    # 7. 보안 표현 (Security-aware Representation)
    # ============================================================

    def __repr__(self) -> str:
        return (
            f"<Signal id={self.signal_id[:8]}... "
            f"strategy={self.strategy_name}@{self.strategy_version} "
            f"symbol={self.symbol} {self.action.value}/{self.strength.value}>"
        )


# ============================================================
# 8. SignalBatch — 배치 컨테이너
# ============================================================

class SignalBatch(BaseModel):
    """한 번의 시그널 생성 사이클에서 산출된 시그널 묶음.

    - HOLD 시그널 포함 가능 (운영 통계용)
    - 빈 배치(signals=[]) 도 유효 (전략이 평가했으나 행동 없음)
    """
    model_config = {
        "frozen": True,
        "validate_assignment": True,
        "extra": "forbid",
    }

    batch_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="배치 식별자 (UUID)",
    )
    strategy_name: str = Field(..., min_length=1, max_length=64)
    strategy_version: str = Field(..., min_length=1, max_length=32)
    generated_at_utc: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="배치 생성 시각 (UTC tz-aware)",
    )
    signals: tuple[Signal, ...] = Field(
        default_factory=tuple,
        description="배치에 포함된 시그널 (불변)",
    )
    universe_size: int = Field(
        default=0,
        ge=0,
        description="평가 대상 종목 수 (HOLD 포함). 운영 통계용.",
    )
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
            raise ValueError(
                f"metadata key {bad!r} matches a secret keyword; refused"
            )
        return v

    @model_validator(mode="after")
    def _validate_batch_consistency(self) -> "SignalBatch":
        # 배치의 모든 시그널이 동일 strategy_name/version 이어야 함
        for s in self.signals:
            if s.strategy_name != self.strategy_name:
                raise ValueError(
                    f"signal {s.signal_id[:8]} strategy_name {s.strategy_name!r} "
                    f"does not match batch {self.strategy_name!r}"
                )
            if s.strategy_version != self.strategy_version:
                raise ValueError(
                    f"signal {s.signal_id[:8]} strategy_version "
                    f"{s.strategy_version!r} does not match "
                    f"batch {self.strategy_version!r}"
                )
        # universe_size 는 actionable + HOLD 합 이상이어야 함 (배치는 부분집합 가능)
        if self.universe_size < len(self.signals):
            raise ValueError(
                f"universe_size ({self.universe_size}) must be >= "
                f"number of signals ({len(self.signals)})"
            )
        return self

    # ============================================================
    # 9. 헬퍼
    # ============================================================

    def actionable_signals(self) -> tuple[Signal, ...]:
        """HOLD 가 아닌 시그널만 반환."""
        return tuple(s for s in self.signals if s.is_actionable())

    def hold_count(self) -> int:
        """HOLD 시그널 개수."""
        return sum(1 for s in self.signals if s.action == SignalAction.HOLD)

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
    "SignalAction",
    "SignalStrength",
    "Signal",
    "SignalBatch",
]
