"""ActionKind — Phase 2A 전용 모듈.

Phase 1 approval_store.py에 ActionKind Enum이 없으므로,
Phase 2A 코드가 사용하는 action kind 상수를 여기서 정의합니다.

Phase 1 ApprovalRecord.action_kind 필드가 str 타입일 경우,
ActionKind(str, Enum)으로 정의하여 문자열 비교가 자동으로 작동합니다.
예: record.action_kind == ActionKind.SUBMIT_ORDER
    → record.action_kind == "submit_order"  (동일)
"""

from __future__ import annotations
import enum


class ActionKind(str, enum.Enum):
    """Phase 2A 액션 종류. str 상속으로 Phase 1 str 필드와 호환."""
    SUBMIT_ORDER = "submit_order"
    CANCEL_ORDER = "cancel_order"
    SET_CAPACITY = "set_capacity"
    KILL_SWITCH = "kill_switch"
