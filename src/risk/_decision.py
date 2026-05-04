"""사전 리스크 게이트 의사결정 타입 (Risk gate decision types).

CheckResult: 단일 검사 결과 (PASS / REJECT + 사유 + 컨텍스트)
GateDecision: 게이트 전체 결정 (모든 검사 결과 보존)
RejectionReason: risk_limits.yaml §10.4 의 17개 사유 분류

설계 원칙:
- Immutable (frozen dataclass)
- 모든 결정에 추적 가능한 컨텍스트 포함 (수치, 임계값 등)
- 시크릿 미포함 — 결정 컨텍스트는 로깅·리포트 입력으로 사용
- decided_at 은 항상 UTC tz-aware

관련 모듈:
- src/risk/risk_gate.py — 본 타입을 반환하는 게이트 본체
- configs/risk_limits.example.yaml §10.4 rejection_reason_taxonomy
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


# ============================================================
# 거부 사유 분류 (Rejection reason taxonomy)
# ============================================================
# risk_limits.yaml §10.4 와 1:1 일치. 신규 사유 추가 시 yaml 도 함께 수정.
class RejectionReason(str, Enum):
    EMERGENCY_STOP         = "EMERGENCY_STOP"
    KILL_SWITCH            = "KILL_SWITCH"
    MARKET_CLOSED          = "MARKET_CLOSED"
    MARKET_STATE_GUARD     = "MARKET_STATE_GUARD"
    CAPACITY_BREACH        = "CAPACITY_BREACH"
    LOSS_LIMIT_BREACH      = "LOSS_LIMIT_BREACH"
    POSITION_LIMIT_BREACH  = "POSITION_LIMIT_BREACH"
    ORDER_FREQUENCY_BREACH = "ORDER_FREQUENCY_BREACH"
    DUPLICATE_ORDER        = "DUPLICATE_ORDER"
    SELF_CROSS             = "SELF_CROSS"
    WHIPSAW_GUARD          = "WHIPSAW_GUARD"
    SLIPPAGE_TOO_LARGE     = "SLIPPAGE_TOO_LARGE"
    PRICE_SANITY_FAIL      = "PRICE_SANITY_FAIL"
    SPREAD_TOO_WIDE        = "SPREAD_TOO_WIDE"
    QUOTE_STALE            = "QUOTE_STALE"
    VOLUME_INSUFFICIENT    = "VOLUME_INSUFFICIENT"
    VALIDATION_ERROR       = "VALIDATION_ERROR"


# ============================================================
# 단일 검사 결과 (Single check result)
# ============================================================
@dataclass(frozen=True)
class CheckResult:
    """단일 검사의 결정 — immutable.

    Attributes:
        passed:     검사 통과 여부.
        check_name: 검사 식별자 (e.g. "kill_switch_check").
        reason:     passed=False 일 때 거부 사유 (RejectionReason).
        detail:     사람이 읽을 수 있는 짧은 설명 (시크릿 미포함).
        context:    수치·임계값 등 추적 정보 (직렬화 가능 형식 권장).
    """
    passed: bool
    check_name: str
    reason: Optional[RejectionReason] = None
    detail: str = ""
    context: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def pass_(cls, check_name: str, **context: Any) -> "CheckResult":
        """통과 결과 생성 헬퍼."""
        return cls(passed=True, check_name=check_name, context=dict(context))

    @classmethod
    def reject(
        cls,
        check_name: str,
        reason: RejectionReason,
        detail: str = "",
        **context: Any,
    ) -> "CheckResult":
        """거부 결과 생성 헬퍼."""
        return cls(
            passed=False,
            check_name=check_name,
            reason=reason,
            detail=detail,
            context=dict(context),
        )


# ============================================================
# 게이트 전체 결정 (Gate decision)
# ============================================================
@dataclass(frozen=True)
class GateDecision:
    """사전 리스크 게이트 평가 결과.

    REJECT 시: 첫 실패 검사의 reason/detail 이 채워지고, 후속 검사는 미실행.
    PASS 시: 모든 검사 결과가 check_results 에 보존됨.

    Attributes:
        approved:         최종 승인 여부.
        client_order_id:  주문 추적용 ID (intent.client_order_id 그대로).
        decided_at:       UTC tz-aware datetime — 결정 시각.
        rejection_reason: REJECT 시 사유 (PASS 시 None).
        rejection_detail: REJECT 시 상세 메시지.
        failed_check:     REJECT 시 실패한 check_name.
        check_results:    모든 검사 결과 (PASS 검사들도 포함; short-circuit 시 부분 목록).
    """
    approved: bool
    client_order_id: str
    decided_at: datetime
    rejection_reason: Optional[RejectionReason] = None
    rejection_detail: str = ""
    failed_check: Optional[str] = None
    check_results: tuple[CheckResult, ...] = ()

    @property
    def is_rejected(self) -> bool:
        return not self.approved

    def __repr__(self) -> str:
        if self.approved:
            return (
                f"<GateDecision APPROVED order={self.client_order_id} "
                f"checks={len(self.check_results)}>"
            )
        return (
            f"<GateDecision REJECTED order={self.client_order_id} "
            f"reason={self.rejection_reason.value if self.rejection_reason else 'None'} "
            f"failed={self.failed_check!r}>"
        )
