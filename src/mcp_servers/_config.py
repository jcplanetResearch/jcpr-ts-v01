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
