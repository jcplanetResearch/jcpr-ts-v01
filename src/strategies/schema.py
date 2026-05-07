"""
전략 레지스트리 스키마 (Strategy Registry Schema)
==================================================

JCPR Trading System - jcpr-ts-v01
Task 45 v0.1

YAML 파일에서 로드되는 전략 메타데이터의 Pydantic 모델.
(Pydantic models for strategy metadata loaded from YAML.)

설계 원칙 (Design Principles):
    - 엄격한 검증 (strict validation) — 잘못된 형식 즉시 거부
    - 시크릿 차단 — parameters에 시크릿 키워드 발견 시 ValueError
    - module_path 화이트리스트 — src.signals.strategies.* 만 허용
    - 자본 가중치 합 검증은 RegistryFile에서
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Any, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


# ─────────────────────────────────────────────────
# 상수 (Constants)
# ─────────────────────────────────────────────────

# 허용된 module_path prefix — 임의 코드 실행 차단
ALLOWED_MODULE_PREFIXES = (
    "src.signals.strategies.",
)

# 허용된 timeframe (Task 14 v0.3과 호환)
ALLOWED_TIMEFRAMES = frozenset({
    "1m", "3m", "5m", "15m", "30m",
    "1h", "4h",
    "1d", "1w", "1M",
})

# 허용된 SignalCategory (Task 15 v0.2와 호환)
ALLOWED_SIGNAL_CATEGORIES = frozenset({
    "STOP_LOSS",
    "RISK_REDUCE",
    "EXIT",
    "REBALANCE",
    "ENTRY",
})

# strategy_id 패턴 — 알파벳/숫자/언더스코어/하이픈만
STRATEGY_ID_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9_\-]*$")

# class_name 패턴 — 파이썬 클래스명 규칙
CLASS_NAME_PATTERN = re.compile(r"^[A-Z][a-zA-Z0-9_]*$")

# 시맨틱 버전 (semver)
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")

# 시크릿 키워드 — parameters에서 차단
SECRET_KEYWORDS = (
    "api_key", "apikey", "api-key",
    "secret", "secret_key",
    "password", "passwd",
    "token", "bearer",
    "private_key", "privatekey",
    "auth", "authorization",
    "credential",
)


# ─────────────────────────────────────────────────
# 검증 헬퍼 (Validation Helpers)
# ─────────────────────────────────────────────────

def _check_no_secret_keys(d: dict[str, Any], path: str = "") -> None:
    """dict 키·문자열 값에 시크릿 키워드 발견 시 ValueError."""
    for k, v in d.items():
        full = f"{path}.{k}" if path else str(k)
        # 키 검사
        low = str(k).lower()
        for kw in SECRET_KEYWORDS:
            if kw in low:
                raise ValueError(
                    f"parameters key '{full}' contains forbidden keyword '{kw}' "
                    f"(시크릿은 환경변수로만 전달 가능)"
                )
        # 값 검사 (재귀)
        if isinstance(v, dict):
            _check_no_secret_keys(v, full)
        elif isinstance(v, str):
            # 긴 base64-like 문자열은 의심
            if len(v) >= 32 and re.match(r"^[A-Za-z0-9+/=_\-]+$", v):
                raise ValueError(
                    f"parameters value at '{full}' looks like a credential "
                    f"(긴 영숫자 문자열 — 시크릿 의심)"
                )


# ─────────────────────────────────────────────────
# StrategyEntry — 단일 전략 메타데이터
# ─────────────────────────────────────────────────

class StrategyEntry(BaseModel):
    """
    단일 전략의 레지스트리 엔트리.

    YAML의 strategies: 항목 하나가 이 모델로 검증된다.
    """

    model_config = ConfigDict(
        extra="forbid",         # 정의되지 않은 필드 거부
        frozen=True,            # 변경 불가
        str_strip_whitespace=True,
    )

    # ─── 식별자 (Identity) ────────────────────
    strategy_id: str = Field(
        ...,
        description="고유 전략 ID (alphanumeric + _ -)",
        min_length=2,
        max_length=64,
    )
    module_path: str = Field(
        ...,
        description="Python module path (must start with src.signals.strategies.)",
        min_length=1,
    )
    class_name: str = Field(
        ...,
        description="Strategy class name (PascalCase)",
        min_length=1,
        max_length=64,
    )
    version: str = Field(
        ...,
        description="Semantic version (e.g., 1.0.0)",
    )

    # ─── 활성화 (Activation) ──────────────────
    enabled: bool = Field(default=False, description="활성화 여부")
    paper_only: bool = Field(
        default=True,
        description="페이퍼 트레이딩만 허용 (라이브 차단)",
    )

    # ─── 자본 할당 (Capital — Task 46이 사용) ─
    capital_weight: Decimal = Field(
        default=Decimal("0"),
        ge=Decimal("0"),
        le=Decimal("1"),
        description="자본 할당 가중치 (0.0-1.0)",
    )
    max_capital_pct: Decimal = Field(
        default=Decimal("0.1"),
        ge=Decimal("0"),
        le=Decimal("1"),
        description="단일 전략 최대 자본 비율 (0.0-1.0)",
    )

    # ─── 운영 메타 (Operational Meta) ─────────
    timeframe: str = Field(
        ...,
        description="Bar timeframe (1m/5m/1h/1d 등 — Task 14 v0.3 호환)",
    )
    universe: list[str] = Field(
        default_factory=list,
        description="허용 종목 화이트리스트 (빈 리스트 = 전체 허용)",
    )
    signal_categories: list[str] = Field(
        default_factory=list,
        min_length=0,
        description="발행 가능 SignalCategory 화이트리스트",
    )

    # ─── 파라미터 (Parameters) ────────────────
    parameters: dict[str, Any] = Field(
        default_factory=dict,
        description="전략별 자유 형식 파라미터 (시크릿 금지)",
    )

    # ─── 라이프사이클 (Lifecycle) ─────────────
    activated_at: Optional[date] = Field(
        default=None,
        description="활성화 일자 (KST)",
    )
    notes: str = Field(
        default="",
        max_length=1000,
        description="운영자 메모",
    )

    # ─────────────────────────────────────────
    # Validators
    # ─────────────────────────────────────────

    @field_validator("strategy_id")
    @classmethod
    def _validate_strategy_id(cls, v: str) -> str:
        if not STRATEGY_ID_PATTERN.match(v):
            raise ValueError(
                f"strategy_id '{v}' invalid — 알파벳으로 시작, "
                f"alphanumeric/underscore/hyphen만 허용"
            )
        return v

    @field_validator("module_path")
    @classmethod
    def _validate_module_path(cls, v: str) -> str:
        if not any(v.startswith(prefix) for prefix in ALLOWED_MODULE_PREFIXES):
            raise ValueError(
                f"module_path '{v}' not allowed — must start with "
                f"one of: {ALLOWED_MODULE_PREFIXES} (보안: 임의 코드 실행 차단)"
            )
        # 추가 검증: 점·알파벳·숫자·언더스코어만
        if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_.]*$", v):
            raise ValueError(f"module_path '{v}' contains invalid characters")
        return v

    @field_validator("class_name")
    @classmethod
    def _validate_class_name(cls, v: str) -> str:
        if not CLASS_NAME_PATTERN.match(v):
            raise ValueError(
                f"class_name '{v}' invalid — PascalCase only "
                f"(대문자로 시작, alphanumeric+underscore)"
            )
        return v

    @field_validator("version")
    @classmethod
    def _validate_version(cls, v: str) -> str:
        if not VERSION_PATTERN.match(v):
            raise ValueError(
                f"version '{v}' invalid — semantic version (X.Y.Z) required"
            )
        return v

    @field_validator("timeframe")
    @classmethod
    def _validate_timeframe(cls, v: str) -> str:
        if v not in ALLOWED_TIMEFRAMES:
            raise ValueError(
                f"timeframe '{v}' not in allowed: {sorted(ALLOWED_TIMEFRAMES)}"
            )
        return v

    @field_validator("signal_categories")
    @classmethod
    def _validate_signal_categories(cls, v: list[str]) -> list[str]:
        invalid = [c for c in v if c not in ALLOWED_SIGNAL_CATEGORIES]
        if invalid:
            raise ValueError(
                f"signal_categories invalid: {invalid} — "
                f"allowed: {sorted(ALLOWED_SIGNAL_CATEGORIES)}"
            )
        # 중복 제거
        return list(dict.fromkeys(v))

    @field_validator("universe")
    @classmethod
    def _validate_universe(cls, v: list[str]) -> list[str]:
        # 종목 코드는 알파벳/숫자만 (KRX 6자리 + 미국 티커 호환)
        for sym in v:
            if not re.match(r"^[A-Z0-9._\-]{1,16}$", sym):
                raise ValueError(f"universe symbol '{sym}' invalid format")
        # 중복 제거
        return list(dict.fromkeys(v))

    @field_validator("parameters")
    @classmethod
    def _validate_parameters(cls, v: dict[str, Any]) -> dict[str, Any]:
        _check_no_secret_keys(v)
        return v

    @model_validator(mode="after")
    def _validate_post(self) -> "StrategyEntry":
        # 활성화된 전략은 자본 가중치 > 0이어야 의미 있음 (경고 수준 — 거부 안 함)
        # paper_only=True인데 max_capital_pct가 큰 경우는 허용 (페이퍼는 어차피 가상)
        return self

    # ─────────────────────────────────────────
    # 동적 클래스 로더 (옵션 C: 화이트리스트 검증 후 import)
    # ─────────────────────────────────────────

    def load_class(self) -> type:
        """
        module_path + class_name으로 클래스를 동적 import.

        보안 (Security):
            - module_path는 이미 화이트리스트 검증됨
            - importlib을 통한 정상 import만 사용
            - eval/exec 사용 안 함

        Returns:
            전략 클래스 타입

        Raises:
            ImportError: 모듈 없음
            AttributeError: 클래스 없음
        """
        import importlib
        # 한 번 더 prefix 확인 (defense in depth)
        if not any(self.module_path.startswith(p) for p in ALLOWED_MODULE_PREFIXES):
            raise ImportError(
                f"module_path '{self.module_path}' not in whitelist"
            )
        module = importlib.import_module(self.module_path)
        cls = getattr(module, self.class_name, None)
        if cls is None:
            raise AttributeError(
                f"Class '{self.class_name}' not found in {self.module_path}"
            )
        if not isinstance(cls, type):
            raise TypeError(
                f"'{self.class_name}' in {self.module_path} is not a class"
            )
        return cls

    def __repr__(self) -> str:
        # 시크릿 가능성이 있는 parameters는 마스킹
        return (
            f"StrategyEntry(id={self.strategy_id!r}, "
            f"version={self.version!r}, "
            f"enabled={self.enabled}, "
            f"paper_only={self.paper_only}, "
            f"timeframe={self.timeframe!r})"
        )


# ─────────────────────────────────────────────────
# RegistryFile — 전체 YAML 파일 모델
# ─────────────────────────────────────────────────

class RegistryFile(BaseModel):
    """전체 strategy_registry.yaml 파일 모델."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(..., description="레지스트리 파일 스키마 버전")
    strategies: list[StrategyEntry] = Field(default_factory=list)

    @field_validator("version")
    @classmethod
    def _check_version(cls, v: str) -> str:
        if not re.match(r"^\d+\.\d+$", v):
            raise ValueError(f"file version '{v}' invalid (expected X.Y)")
        return v

    @model_validator(mode="after")
    def _check_unique_ids(self) -> "RegistryFile":
        seen = set()
        for s in self.strategies:
            if s.strategy_id in seen:
                raise ValueError(
                    f"duplicate strategy_id: {s.strategy_id!r}"
                )
            seen.add(s.strategy_id)
        return self

    @model_validator(mode="after")
    def _check_capital_weight_sum(self) -> "RegistryFile":
        """활성 전략의 capital_weight 합 ≤ 1.0 검증."""
        active_sum = sum(
            (s.capital_weight for s in self.strategies if s.enabled),
            Decimal(0),
        )
        if active_sum > Decimal("1.0"):
            raise ValueError(
                f"sum of capital_weight for enabled strategies "
                f"= {active_sum} (must be ≤ 1.0)"
            )
        return self
