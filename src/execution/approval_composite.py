"""
복합 승인 제공자 (Composite Approval Provider)
================================================

JCPR Trading System - jcpr-ts-v01
Task 40 v0.1

여러 ApprovalProvider를 순차로 시도. 첫 승인을 반환, 모두 거부면 마지막 거부 반환.
(Tries multiple providers in order — returns first approval, or last rejection.)

전형적 사용 예:
    composite = CompositeApprovalProvider([
        PreApprovalProvider(manager),  # 사전 윈도우 매칭 시 자동 승인
        CLIApprovalProvider(),          # 폴백 — 인간 응답
    ])
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from .approval import ApprovalDecision, ApprovalProvider, ApprovalRequest

logger = logging.getLogger(__name__)


class CompositeApprovalProvider(ApprovalProvider):
    """
    여러 Provider를 순차 시도.
    
    동작:
    1. 첫 Provider 호출 → approved=True 반환 시 즉시 종료
    2. approved=False면 다음 Provider 시도
    3. 모두 거부 → 마지막 Provider의 결과 반환
    
    중간 Provider가 예외 발생 시:
    - log warning + 다음 Provider로 진행 (resilient)
    - 모든 Provider 예외 → 거부 반환
    """

    name = "composite"

    def __init__(
        self,
        providers: list[ApprovalProvider],
        *,
        short_circuit_on_first_approve: bool = True,
    ):
        if not providers:
            raise ValueError("providers 비어있음 — 최소 1개 필요")
        self._providers = list(providers)
        self._short_circuit = short_circuit_on_first_approve

    def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        last_decision: Optional[ApprovalDecision] = None
        chain_log: list[str] = []
        last_exception: Optional[Exception] = None

        for provider in self._providers:
            provider_name = getattr(provider, "name", type(provider).__name__)
            try:
                decision = provider.request_approval(request)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "Provider %s 예외 발생, 다음으로 진행: %s",
                    provider_name, e,
                )
                last_exception = e
                chain_log.append(f"{provider_name}:error")
                continue

            chain_log.append(f"{provider_name}:{'approved' if decision.approved else 'rejected'}")
            last_decision = decision

            if decision.approved and self._short_circuit:
                # composite 메타데이터 추가
                return ApprovalDecision(
                    approved=True,
                    reason=decision.reason,
                    decided_at_utc=decision.decided_at_utc,
                    approver=f"{self.name}({decision.approver})",
                    metadata={
                        **dict(decision.metadata),
                        "chain": chain_log,
                    },
                )

        # 모두 거부 또는 예외
        if last_decision is None:
            # 모든 Provider 예외 — fail-closed
            return ApprovalDecision(
                approved=False,
                reason=(
                    f"all providers failed with exceptions: "
                    f"last={type(last_exception).__name__ if last_exception else 'unknown'}"
                ),
                decided_at_utc=datetime.now(timezone.utc),
                approver=self.name,
                metadata={"chain": chain_log},
            )

        # 마지막 Provider의 거부 사유 + chain 추가
        return ApprovalDecision(
            approved=False,
            reason=last_decision.reason,
            decided_at_utc=last_decision.decided_at_utc,
            approver=f"{self.name}({last_decision.approver})",
            metadata={
                **dict(last_decision.metadata),
                "chain": chain_log,
            },
        )
