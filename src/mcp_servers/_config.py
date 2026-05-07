"""
MCP 서버 설정 (MCP Server Config)
==================================

JCPR Trading System - jcpr-ts-v01
Task 34 v0.1

서버 시작 시 설정 검증 + 환경변수 로드.
(Validates config + loads from env vars at startup.)

설계 (Design):
    - 자격증명 절대 저장 안 함 (no credentials)
    - 모든 경로는 read-only로 사용됨 (mode=ro)
    - 환경변수 우선, 없으면 기본값
    - Pydantic 엄격 검증
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ─────────────────────────────────────────────────
# 환경변수 키 (Environment Variables)
# ─────────────────────────────────────────────────

ENV_AUDIT_DIR = "JCPR_AUDIT_DIR"
ENV_POSITIONS_DB = "JCPR_POSITIONS_DB"
ENV_OHLCV_DB = "JCPR_OHLCV_DB"
ENV_QUOTE_DB = "JCPR_QUOTE_DB"
ENV_RISK_AUDIT = "JCPR_RISK_AUDIT"
ENV_EXEC_AUDIT = "JCPR_EXEC_AUDIT"
ENV_STRATEGY_REGISTRY = "JCPR_STRATEGY_REGISTRY"
ENV_SESSION_ID = "JCPR_SESSION_ID"

# Task 35 — Restricted server
ENV_APPROVAL_DB = "JCPR_APPROVAL_DB"
ENV_ALLOW_LIVE = "JCPR_ALLOW_LIVE"
ENV_OPERATOR_ID = "JCPR_OPERATOR_ID"

# 자격증명 의심 환경변수 — 실수로 사용 못하게 차단 검증
FORBIDDEN_ENV_KEYWORDS = (
    "PASSWORD", "SECRET", "TOKEN", "API_KEY",
    "AUTH", "CREDENTIAL", "PRIVATE_KEY",
)


# ─────────────────────────────────────────────────
# 설정 모델 (Config Model)
# ─────────────────────────────────────────────────

class ReadOnlyServerConfig(BaseModel):
    """MCP read-only 서버 설정.

    모든 필드는 read-only 데이터 경로 또는 audit 출력 경로.
    자격증명 필드 없음.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    # ─── 출력 (Audit) ─────────────────────────
    audit_dir: str = Field(
        default="data/audit",
        description="Task A2 AuditWriter 출력 디렉터리 — write 가능",
    )

    # ─── 입력 (Read-only data) ────────────────
    positions_db: Optional[str] = Field(
        default=None,
        description="Task 25 positions DB — read-only",
    )
    ohlcv_db: Optional[str] = Field(
        default=None,
        description="Task 12 OHLCV DB — read-only",
    )
    quote_db: Optional[str] = Field(
        default=None,
        description="Task 13 quote DB — read-only",
    )
    risk_audit_path: Optional[str] = Field(
        default=None,
        description="Task 19 risk_decisions.jsonl — read-only",
    )
    execution_audit_path: Optional[str] = Field(
        default=None,
        description="Task 21 executions.jsonl — read-only",
    )
    strategy_registry_path: Optional[str] = Field(
        default=None,
        description="Task 45 strategy_registry.yaml — read-only",
    )

    # ─── 세션 ─────────────────────────────────
    session_id: str = Field(
        default="mcp-session-default",
        min_length=2,
        max_length=64,
        description="세션 식별자 (Task A1 trace 연결용)",
    )

    # ─── Rate Limit ───────────────────────────
    rate_limit_per_minute: int = Field(
        default=120,
        ge=1,
        le=10000,
        description="분당 도구 호출 한도",
    )

    # ─── 보안 ─────────────────────────────────
    enable_get_trace: bool = Field(
        default=True,
        description="get_trace 도구 활성화 (audit log 노출)",
    )
    max_trace_events_returned: int = Field(
        default=200,
        ge=1,
        le=10000,
        description="get_trace 최대 반환 이벤트",
    )
    max_fills_returned: int = Field(
        default=500,
        ge=1,
        le=5000,
        description="get_recent_fills 최대 반환",
    )

    # ─────────────────────────────────────────
    # 검증
    # ─────────────────────────────────────────

    @field_validator("session_id")
    @classmethod
    def _validate_session_id(cls, v: str) -> str:
        # alphanumeric + - _
        import re
        if not re.match(r"^[a-zA-Z0-9_\-]+$", v):
            raise ValueError(
                f"session_id '{v}' invalid — "
                f"alphanumeric/underscore/hyphen만 허용"
            )
        return v


# ─────────────────────────────────────────────────
# 환경변수 로더 (Env Loader)
# ─────────────────────────────────────────────────

def load_config_from_env() -> ReadOnlyServerConfig:
    """
    환경변수에서 설정 로드.

    보안 (Security):
        - 자격증명성 환경변수가 set 되어 있으면 ValueError
          (실수로 자격증명을 MCP 서버에 노출하는 것 차단)

    Returns:
        ReadOnlyServerConfig
    """
    # 1. 자격증명성 env var 사전 차단
    _check_no_credential_env_in_jcpr_namespace()

    # 2. 환경변수 → config dict
    raw: dict[str, object] = {
        "audit_dir": os.environ.get(ENV_AUDIT_DIR, "data/audit"),
    }
    for key, env in [
        ("positions_db", ENV_POSITIONS_DB),
        ("ohlcv_db", ENV_OHLCV_DB),
        ("quote_db", ENV_QUOTE_DB),
        ("risk_audit_path", ENV_RISK_AUDIT),
        ("execution_audit_path", ENV_EXEC_AUDIT),
        ("strategy_registry_path", ENV_STRATEGY_REGISTRY),
    ]:
        v = os.environ.get(env)
        if v:
            raw[key] = v

    if ENV_SESSION_ID in os.environ:
        raw["session_id"] = os.environ[ENV_SESSION_ID]

    return ReadOnlyServerConfig(**raw)


def _check_no_credential_env_in_jcpr_namespace() -> None:
    """JCPR_ prefix의 환경변수 중 자격증명성이 있으면 거부."""
    for env_name in os.environ:
        if not env_name.startswith("JCPR_"):
            continue
        upper = env_name.upper()
        for kw in FORBIDDEN_ENV_KEYWORDS:
            if kw in upper:
                raise ValueError(
                    f"환경변수 '{env_name}'은 자격증명 의심 — "
                    f"MCP 서버는 자격증명을 절대 처리하지 않음. "
                    f"unset 후 재실행하세요."
                )


# ─────────────────────────────────────────────────
# Task 35 — Restricted Server Config
# ─────────────────────────────────────────────────

class RestrictedServerConfig(BaseModel):
    """MCP restricted (write) 서버 설정.

    Task 34와 비교한 차이:
        - approval_db: SQLite 승인 저장소 경로 (필수)
        - allow_live: live 모드 허용 (기본 False — paper_only 강제)
        - operator_id: 승인 처리할 운영자 ID (self-approval 차단용)
        - rate_limit_per_minute: 30 (write는 read보다 엄격)
        - approval_ttl_seconds: 승인 요청 TTL
        - execute_ttl_seconds: 승인 후 실행 TTL
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    # ─── 출력 (Audit + DB) ────────────────────
    audit_dir: str = Field(
        default="data/audit",
        description="Task A2 AuditWriter 출력 디렉터리",
    )
    approval_db: str = Field(
        default="data/approvals.sqlite",
        description="승인 저장소 SQLite 파일",
    )

    # ─── 입력 (Read-only data — write 도구가 검증 시 사용) ─
    positions_db: Optional[str] = Field(default=None)
    strategy_registry_path: Optional[str] = Field(default=None)
    risk_audit_path: Optional[str] = Field(default=None)

    # ─── 세션 ─────────────────────────────────
    session_id: str = Field(
        default="restricted-mcp-default",
        min_length=2,
        max_length=64,
    )
    operator_id: str = Field(
        default="operator-default",
        min_length=2,
        max_length=64,
        description="승인 처리할 운영자 ID — self-approval 차단",
    )

    # ─── Live/Paper ───────────────────────────
    allow_live: bool = Field(
        default=False,
        description=(
            "Live 모드 허용 여부 — 기본 False (paper-only 강제). "
            "True로 설정해도 요청 시 mode='live' 명시 필요."
        ),
    )

    # ─── Rate Limit (write는 더 엄격) ─────────
    rate_limit_per_minute: int = Field(
        default=30,
        ge=1,
        le=1000,
        description="분당 도구 호출 한도 (write는 30/min)",
    )

    # ─── 승인 TTL ─────────────────────────────
    approval_ttl_seconds: int = Field(
        default=300,
        ge=5,
        le=86400,
        description="요청 후 승인 받기까지 TTL (기본 5분)",
    )
    execute_ttl_seconds: int = Field(
        default=60,
        ge=5,
        le=3600,
        description="승인 후 실행까지 TTL (기본 1분)",
    )

    # ─── 출력 한도 ────────────────────────────
    max_pending_returned: int = Field(
        default=100,
        ge=1,
        le=1000,
    )

    # ─── 보안 ─────────────────────────────────
    allow_self_approval: bool = Field(
        default=False,
        description="self-approval 허용 (테스트용 — 운영시 False 권장)",
    )

    # ─────────────────────────────────────────
    # 검증
    # ─────────────────────────────────────────

    @field_validator("session_id", "operator_id")
    @classmethod
    def _validate_id(cls, v: str) -> str:
        import re
        if not re.match(r"^[a-zA-Z0-9_\-]+$", v):
            raise ValueError(
                f"id '{v}' invalid — alphanumeric/underscore/hyphen만 허용"
            )
        return v


def load_restricted_config_from_env() -> RestrictedServerConfig:
    """
    환경변수에서 restricted 설정 로드.

    Returns:
        RestrictedServerConfig
    """
    _check_no_credential_env_in_jcpr_namespace()

    raw: dict[str, object] = {
        "audit_dir": os.environ.get(ENV_AUDIT_DIR, "data/audit"),
    }
    # approval_db
    if ENV_APPROVAL_DB in os.environ:
        raw["approval_db"] = os.environ[ENV_APPROVAL_DB]

    # 데이터 경로
    for key, env in [
        ("positions_db", ENV_POSITIONS_DB),
        ("strategy_registry_path", ENV_STRATEGY_REGISTRY),
        ("risk_audit_path", ENV_RISK_AUDIT),
    ]:
        v = os.environ.get(env)
        if v:
            raw[key] = v

    # 세션/운영자
    if ENV_SESSION_ID in os.environ:
        raw["session_id"] = os.environ[ENV_SESSION_ID]
    if ENV_OPERATOR_ID in os.environ:
        raw["operator_id"] = os.environ[ENV_OPERATOR_ID]

    # Live 모드 — 명시적 "1" 또는 "true" 만 허용
    allow_live_raw = os.environ.get(ENV_ALLOW_LIVE, "0").strip().lower()
    raw["allow_live"] = allow_live_raw in ("1", "true", "yes")

    return RestrictedServerConfig(**raw)
