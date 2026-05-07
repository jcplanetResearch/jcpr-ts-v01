"""
MCP 서버 패키지 (MCP Servers Package)
======================================

JCPR Trading System - jcpr-ts-v01

Task 34 v0.1 — Read-only server (8 tools, stdio, no secrets)
Task 35 v0.1 — Restricted server (8 write tools + 2 internal, human approval)
"""

from ._approval_store import (
    ACTION_CANCEL_ORDER,
    ACTION_KILL_SWITCH,
    ACTION_SET_CAPACITY,
    ACTION_SUBMIT_ORDER,
    ALL_STATUSES,
    ALLOWED_ACTIONS,
    STATUS_APPROVED,
    STATUS_CANCELLED,
    STATUS_EXECUTED,
    STATUS_EXPIRED,
    STATUS_PENDING,
    STATUS_REJECTED,
    ApprovalNotFound,
    ApprovalRecord,
    ApprovalStateError,
    ApprovalStore,
    ApprovalStoreError,
    SelfApprovalError,
    generate_approval_id,
)
from ._config import (
    ENV_ALLOW_LIVE,
    ENV_APPROVAL_DB,
    ENV_AUDIT_DIR,
    ENV_OHLCV_DB,
    ENV_OPERATOR_ID,
    ENV_POSITIONS_DB,
    ENV_QUOTE_DB,
    ENV_RISK_AUDIT,
    ENV_SESSION_ID,
    ENV_STRATEGY_REGISTRY,
    ReadOnlyServerConfig,
    RestrictedServerConfig,
    load_config_from_env,
    load_restricted_config_from_env,
)
from .readonly_server import build_server as build_readonly_server
from .restricted_server import build_server as build_restricted_server

# 하위 호환: Task 34 build_server는 readonly
build_server = build_readonly_server

__all__ = [
    "build_server",
    "build_readonly_server",
    "build_restricted_server",
    "ReadOnlyServerConfig",
    "RestrictedServerConfig",
    "load_config_from_env",
    "load_restricted_config_from_env",
    "ENV_AUDIT_DIR", "ENV_POSITIONS_DB", "ENV_OHLCV_DB", "ENV_QUOTE_DB",
    "ENV_RISK_AUDIT", "ENV_STRATEGY_REGISTRY", "ENV_SESSION_ID",
    "ENV_APPROVAL_DB", "ENV_ALLOW_LIVE", "ENV_OPERATOR_ID",
    "ApprovalStore", "ApprovalRecord",
    "ApprovalStoreError", "ApprovalNotFound", "ApprovalStateError",
    "SelfApprovalError", "generate_approval_id",
    "STATUS_PENDING", "STATUS_APPROVED", "STATUS_REJECTED",
    "STATUS_EXECUTED", "STATUS_EXPIRED", "STATUS_CANCELLED",
    "ALL_STATUSES",
    "ACTION_SUBMIT_ORDER", "ACTION_CANCEL_ORDER",
    "ACTION_SET_CAPACITY", "ACTION_KILL_SWITCH",
    "ALLOWED_ACTIONS",
]

__version__ = "0.2.0"
