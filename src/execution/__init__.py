"""
src/execution/__init__.py — JCPR-ts-v01 (Phase 2A 수정)
========================================================

Phase 1 approval_store.py의 실제 클래스명으로 정정.

이름 매핑 (Phase 2A 초안 → Phase 1 실제):
  ApprovalStatus        → ApprovalState
  ApprovalNotFoundError → ApprovalNotFound
  InvalidTransitionError → ApprovalStateError
  LiveModeNotAllowedError → LiveModeBlockedError
  TTLExpiredError       → ApprovalExpiredError
  ActionKind            → 별도 enum 없음, 호환 alias 추가
"""

from src.execution.approval_store import (  # noqa: F401
    ApprovalRecord,
    ApprovalStore,
    ApprovalStoreError,
    ApprovalState,
    ApprovalNotFound,
    ApprovalStateError,
    SelfApprovalError,
    ApprovalExpiredError,
    ApprovalIntegrityError,
    LiveModeBlockedError,
)

# Phase 2A 코드 호환 alias — Phase 1 이름 → Phase 2A 이름
# (Phase 2A 코드가 이 이름들을 사용하므로 여기서 re-export)
ApprovalStatus = ApprovalState                  # alias
InvalidTransitionError = ApprovalStateError     # alias
InvalidStateTransitionError = ApprovalStateError  # alias
ApprovalNotFoundError = ApprovalNotFound        # alias
TTLExpiredError = ApprovalExpiredError          # alias
ExpiredApprovalError = ApprovalExpiredError     # alias
LiveModeNotAllowedError = LiveModeBlockedError  # alias

from src.execution.execution_gateway import (  # noqa: F401
    ExecutionGateway,
    ExecutionResult,
    GatewayError,
    InterruptedExecutionError,
    LiveModeBlockedError as _LiveModeBlockedError,  # already imported above
)

__all__ = [
    # approval store — Phase 1 실제 이름
    "ApprovalRecord",
    "ApprovalStore",
    "ApprovalStoreError",
    "ApprovalState",
    "ApprovalNotFound",
    "ApprovalStateError",
    "SelfApprovalError",
    "ApprovalExpiredError",
    "ApprovalIntegrityError",
    "LiveModeBlockedError",
    # Phase 2A 호환 alias
    "ApprovalStatus",
    "InvalidTransitionError",
    "InvalidStateTransitionError",
    "ApprovalNotFoundError",
    "TTLExpiredError",
    "ExpiredApprovalError",
    "LiveModeNotAllowedError",
    # gateway
    "ExecutionGateway",
    "ExecutionResult",
    "GatewayError",
    "InterruptedExecutionError",
]
