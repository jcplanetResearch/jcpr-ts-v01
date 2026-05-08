"""src/execution/ — Trading execution & approval workflow.

Phase 1 (this commit): Unified ApprovalStore replaces the two previous stores.
Phase 2 (next): ExecutionGateway integration with MCP write handlers.

This file currently exports ONLY the unified ApprovalStore. Other modules
(ExecutionGateway, OrderRequest, etc.) remain in their original files and
are imported by callers as before until Phase 2.
"""
from __future__ import annotations

from .approval_store import (
    ACTION_CANCEL_ORDER,
    ACTION_KILL_SWITCH,
    ACTION_SET_CAPACITY,
    ACTION_SUBMIT_ORDER,
    DEFAULT_APPROVAL_TTL_SECONDS,
    DEFAULT_EXECUTE_TTL_SECONDS,
    DEFAULT_KILL_SWITCH_TTL_SECONDS,
    VALID_ACTION_KINDS,
    ApprovalExpiredError,
    ApprovalIntegrityError,
    ApprovalNotFound,
    ApprovalRecord,
    ApprovalState,
    ApprovalStateError,
    ApprovalStore,
    ApprovalStoreError,
    LiveModeBlockedError,
    SelfApprovalError,
)

__all__ = (
    # Enums + records
    "ApprovalState",
    "ApprovalRecord",
    # Store
    "ApprovalStore",
    # Constants
    "ACTION_SUBMIT_ORDER",
    "ACTION_CANCEL_ORDER",
    "ACTION_SET_CAPACITY",
    "ACTION_KILL_SWITCH",
    "VALID_ACTION_KINDS",
    "DEFAULT_APPROVAL_TTL_SECONDS",
    "DEFAULT_EXECUTE_TTL_SECONDS",
    "DEFAULT_KILL_SWITCH_TTL_SECONDS",
    # Exceptions
    "ApprovalStoreError",
    "ApprovalNotFound",
    "ApprovalStateError",
    "SelfApprovalError",
    "ApprovalExpiredError",
    "ApprovalIntegrityError",
    "LiveModeBlockedError",
)
