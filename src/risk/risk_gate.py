"""사전 리스크 게이트 (Pre-trade Risk Gate) — Task 19.

모든 주문이 통과해야 하는 결정 지점. risk_limits.yaml §10.1 evaluation_order
와 정확히 일치하는 순서로 9개 검사를 실행. 어느 단계라도 실패 시 즉시
short-circuit 으로 거부 반환.

설계 원칙 (Design):
1. Fail-closed — risk_limits.yaml: gate_behavior.fail_open=false
2. Stop-first — 비상 정지 검사가 항상 먼저
3. Short-circuit — 첫 실패 후 후속 검사 미실행
4. Pure function — evaluate() 는 부수효과 없음 (로깅·DB 쓰기는 caller 책임)

관련 모듈:
- src/risk/_decision.py    — CheckResult, GateDecision
- src/risk/_context.py     — RiskGateContext
- src/risk/_checks.py      — 9개 검사 함수
- src/risk/_history.py     — OrderHistory
- src/execution/execution_gateway.py — Task 21, 본 게이트의 주 호출자
- configs/risk_limits.example.yaml §10.1 evaluation_order
"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Callable, Optional

from ._context import RiskGateContext
from ._decision import CheckResult, GateDecision, RejectionReason
from . import _checks as C


# ============================================================
# 평가 순서 (Evaluation order)
# ============================================================
# risk_limits.yaml §10.1 의 9개 검사와 1:1 일치, 동일 순서.
# 비상 정지(kill_switch + emergency_stop)가 항상 가장 먼저 평가됨.
DEFAULT_CHECK_ORDER: tuple[Callable[[RiskGateContext], CheckResult], ...] = (
    C.check_kill_switch,
    C.check_emergency_stop,
    C.check_market_state,
    C.check_capacity,
    C.check_loss_limits,
    C.check_position_limits,
    C.check_order_frequency,
    C.check_duplicate_conflict,
    C.check_execution_guards,
)


# ============================================================
# 게이트 본체 (RiskGate)
# ============================================================
class RiskGate:
    """사전 리스크 게이트 — 모든 주문이 통과해야 하는 결정 지점.

    Args:
        check_order: 사용할 검사 함수 시퀀스. None 이면 DEFAULT_CHECK_ORDER 사용.
                     테스트나 특수 시나리오에서 부분 집합·재정렬 가능.

    Usage:
        gate = RiskGate()
        decision = gate.evaluate(ctx)
        if decision.approved:
            broker.place_order(intent)
        else:
            log_rejection(decision)
    """

    def __init__(
        self,
        *,
        check_order: Optional[tuple[Callable, ...]] = None,
    ) -> None:
        self._checks = check_order or DEFAULT_CHECK_ORDER
        if not self._checks:
            raise ValueError("RiskGate requires at least one check")

    def evaluate(self, ctx: RiskGateContext) -> GateDecision:
        """주어진 컨텍스트로 게이트 평가.

        Returns:
            GateDecision — approved=True 면 주문 발주 가능.

        Behavior:
            1. 검사를 순서대로 실행
            2. 어느 검사라도 passed=False 면 short-circuit 으로 GateDecision(REJECTED) 반환
            3. 검사 함수가 예외를 던지면 fail-closed REJECT (VALIDATION_ERROR)
            4. 모든 검사 통과 시 GateDecision(APPROVED) 반환
        """
        results: list[CheckResult] = []

        for check in self._checks:
            try:
                result = check(ctx)
            except Exception as e:
                # 검사 함수 자체가 예외를 던지는 경우 — 이론상 발생 안 해야 함
                # (각 check_* 가 try/except 보호) — 그러나 다중 방어로 wrap
                check_name = getattr(check, "__name__", "unknown_check")
                fail = CheckResult.reject(
                    check_name=check_name,
                    reason=RejectionReason.VALIDATION_ERROR,
                    detail=f"unhandled exception: {type(e).__name__}",
                )
                results.append(fail)
                return self._build_rejection(ctx, results, fail)

            results.append(result)
            if not result.passed:
                # Short-circuit: 첫 실패 시 후속 검사 미실행
                return self._build_rejection(ctx, results, result)

        # 모든 검사 통과
        return GateDecision(
            approved=True,
            client_order_id=ctx.intent.client_order_id,
            decided_at=datetime.now(timezone.utc),
            check_results=tuple(results),
        )

    @staticmethod
    def _build_rejection(
        ctx: RiskGateContext,
        results: list[CheckResult],
        failed: CheckResult,
    ) -> GateDecision:
        return GateDecision(
            approved=False,
            client_order_id=ctx.intent.client_order_id,
            decided_at=datetime.now(timezone.utc),
            rejection_reason=failed.reason,
            rejection_detail=failed.detail,
            failed_check=failed.check_name,
            check_results=tuple(results),
        )

    @property
    def num_checks(self) -> int:
        return len(self._checks)

    def __repr__(self) -> str:
        names = [getattr(c, "__name__", "?") for c in self._checks]
        return f"<RiskGate checks={len(names)} order={names}>"
