"""
실행 승인 제공자 (Execution Approval Provider)
================================================

JCPR Trading System - jcpr-ts-v01
Task 21 v0.1

주문 송신 직전 인간 승인을 받는 추상 인터페이스.
(Abstract interface for human approval before order submission.)

구현체 (Implementations):
- AutoApproveProvider:    기본 — 자동 승인 (Task 40 본격 구현 전)
- DenyAllProvider:        모든 요청 거부 (안전 모드)
- HumanApprovalProvider:  Task 40에서 본격 구현 (CLI/Web UI)

원칙 (Principles):
- DryRunGuard와 별개의 두 번째 안전망 (defense in depth)
- 결정 사유 (reason) 항상 기록
- ApprovalDecision은 immutable
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ApprovalRequest:
    """
    승인 요청 데이터 (호출자→Provider).
    (Approval request data — caller → provider.)
    """
    execution_id: str
    signal_id: str
    symbol: str
    side: str
    quantity: int
    price: Decimal
    estimated_cost_krw: Decimal
    is_dry_run: bool                 # DryRunGuard 상태
    is_live_env: bool                # KIS_ENV == live 여부
    requested_at_utc: datetime
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ApprovalDecision:
    """
    승인 결정 (Provider→호출자).
    """
    approved: bool
    reason: str                      # 결정 사유 (audit log)
    decided_at_utc: datetime
    approver: str                    # provider name 또는 인간 사용자 ID
    metadata: dict[str, Any] = field(default_factory=dict)


# ─────────────────────────────────────────────────
# 추상 베이스
# ─────────────────────────────────────────────────

class ApprovalProvider(ABC):
    """승인 제공자 추상 베이스."""

    name: str = "abstract"

    @abstractmethod
    def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        """
        승인 요청. 호출자는 결과의 .approved 확인 후 진행/거부.
        """
        raise NotImplementedError


# ─────────────────────────────────────────────────
# Implementations
# ─────────────────────────────────────────────────

class AutoApproveProvider(ApprovalProvider):
    """
    자동 승인 (Task 40 본격 구현 전 기본값).

    ⚠️ 안전 가드:
    - is_live_env=True 이고 is_dry_run=False 이면 (실거래 활성화) 거부
      → 진짜 실거래는 명시적 HumanApprovalProvider 필요
    """

    name = "auto_approve"

    def __init__(self, *, allow_live: bool = False):
        """
        Args:
            allow_live: 실거래 환경 + live orders도 자동 승인할지.
                False (기본): 실거래는 거부 — 안전.
                True: 모든 경우 자동 승인 (테스트용 권장).
        """
        self._allow_live = allow_live

    def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        now = datetime.now(timezone.utc)

        # 안전 가드: 실거래 + live orders는 자동 승인 거부
        if request.is_live_env and not request.is_dry_run and not self._allow_live:
            logger.warning(
                "AutoApprove 거부 (실거래 + live orders): "
                "execution_id=%s symbol=%s — HumanApprovalProvider 필요",
                request.execution_id, request.symbol,
            )
            return ApprovalDecision(
                approved=False,
                reason=(
                    "실거래 환경에서 live orders는 AutoApprove 불가 "
                    "(use HumanApprovalProvider — Task 40)"
                ),
                decided_at_utc=now,
                approver=self.name,
            )

        return ApprovalDecision(
            approved=True,
            reason=f"auto-approved by {self.name} (dry_run={request.is_dry_run}, "
                   f"live_env={request.is_live_env})",
            decided_at_utc=now,
            approver=self.name,
        )


class DenyAllProvider(ApprovalProvider):
    """모든 요청 거부 — 비상 안전 모드."""

    name = "deny_all"

    def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(
            approved=False,
            reason="DenyAllProvider 활성화 — 모든 주문 거부",
            decided_at_utc=datetime.now(timezone.utc),
            approver=self.name,
        )


class HumanApprovalProvider(ApprovalProvider):
    """
    인간 승인 — Task 40에서 본격 구현.
    이번 v0.1에서는 인터페이스만 정의 + 명시적 미구현 표시.
    """

    name = "human_approval"

    def __init__(self):
        pass

    def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        raise NotImplementedError(
            "HumanApprovalProvider는 Task 40에서 본격 구현됩니다 — "
            "현재는 AutoApproveProvider 또는 DenyAllProvider 사용"
        )
