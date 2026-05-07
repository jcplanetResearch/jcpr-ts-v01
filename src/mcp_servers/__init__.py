"""
MCP 서버 패키지 (MCP Servers Package)
======================================

JCPR Trading System - jcpr-ts-v01

Task 34 v0.1 — Read-only server (8 tools, stdio, no secrets)
Task 35 (예정) — Restricted server (write ops with approval gate)

읽기 전용 서버는 LLM Agent에게 read-only 데이터 접근만 제공.
모든 호출은 Task A1-A3 추적 인프라로 자동 audit.

(Read-only server provides read-only data access to LLM agents.
All calls auto-audited via Task A1-A3 observability.)
"""

from ._config import (
    ENV_AUDIT_DIR,
    ENV_OHLCV_DB,
    ENV_POSITIONS_DB,
    ENV_QUOTE_DB,
    ENV_RISK_AUDIT,
    ENV_SESSION_ID,
    ENV_STRATEGY_REGISTRY,
    ReadOnlyServerConfig,
    load_config_from_env,
)
from .readonly_server import build_server

__all__ = [
    # 빌더
    "build_server",
    # 설정
    "ReadOnlyServerConfig",
    "load_config_from_env",
    # 환경변수 키 (외부 참조용)
    "ENV_AUDIT_DIR",
    "ENV_POSITIONS_DB",
    "ENV_OHLCV_DB",
    "ENV_QUOTE_DB",
    "ENV_RISK_AUDIT",
    "ENV_STRATEGY_REGISTRY",
    "ENV_SESSION_ID",
]

__version__ = "0.1.0"
