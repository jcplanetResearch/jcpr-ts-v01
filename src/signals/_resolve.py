"""src/signals/_resolve.py — Task 16 Stage 5: Resolve (capital conflict).

책임:
- R6: capital conflict resolution (PRIORITY_THEN_FCFS)

설계:
- CapitalEstimator Protocol: Task 18 sizing dry-run 어댑터
- StubCapitalEstimator: 테스트용 고정값/계산식 estimator
- resolve_capital_conflict: 정렬된 시그널을 받아 자본 한도 내에서 수락/거부

알고리즘:
    입력: priority+as_of_utc 정렬된 시그널, available_capital, estimator
    1. remaining = available_capital
    2. for s in sorted:
         cost = estimator.estimate(s)
         if cost <= remaining: accept, remaining -= cost
         else: reject(LOWER_PRIORITY, metadata={available, required})
    3. return (accepted, rejected, capital_consumed)

핵심 보장:
- STOP_LOSS (priority 1) 가 항상 우선 — 자본 부족이면 ENTRY 가 먼저 컷됨
- 동일 priority 내 FCFS (as_of_utc 빠른 순)
- 결정성 (deterministic) — 동일 입력 → 동일 출력
"""
from __future__ import annotations

from decimal import Decimal
from typing import Iterable, Optional, Protocol, runtime_checkable

from src.risk import RejectionReason
from src.signals._decision import RejectedSignal
from src.signals.schema import Signal


# ============================================================
# 1. CapitalEstimator 프로토콜
# ============================================================

@runtime_checkable
class CapitalEstimator(Protocol):
    """자본 요구량 추정 인터페이스.

    실제 구현체는 Task 18 sizing.py 의 dry-run 호출 (Task 18 v0.2 보강 후).
    본 Task 16 에서는 StubCapitalEstimator 로 단독 검증.

    estimate() 는 보수적 (conservative) — 수수료·slippage 포함 추정.
    """

    def estimate(self, signal: Signal) -> Decimal:
        """시그널 수행에 필요한 자본 추정 (KRW 또는 통화).

        반환값:
            >= 0 의 Decimal. 0 = 자본 불필요 (예: SELL/CLOSE — 자본 회수)
        """
        ...


# ============================================================
# 2. Stub 구현 (Task 18 미보강 상태에서 검증용)
# ============================================================

class StubCapitalEstimator:
    """테스트·자체검증용 stub.

    동작:
    - SELL/CLOSE: 0 (자본 회수)
    - BUY: confidence × strength_factor × reference_price × default_qty
    - HOLD: 0

    strength_factor:
        WEAK=1, MEDIUM=2, STRONG=3
    """

    def __init__(self, default_qty: int = 10) -> None:
        if default_qty <= 0:
            raise ValueError("default_qty must be > 0")
        self._default_qty = Decimal(default_qty)

    def estimate(self, signal: Signal) -> Decimal:
        from src.signals.schema import SignalAction, SignalStrength

        if signal.action in (SignalAction.SELL, SignalAction.CLOSE, SignalAction.HOLD):
            return Decimal("0")

        strength_factor = {
            SignalStrength.WEAK: Decimal("1"),
            SignalStrength.MEDIUM: Decimal("2"),
            SignalStrength.STRONG: Decimal("3"),
        }[signal.strength]

        cost = (
            signal.confidence
            * strength_factor
            * signal.reference_price
            * self._default_qty
        )
        return cost.quantize(Decimal("1"))


class FixedCapitalEstimator:
    """모든 시그널에 동일 비용 — 가장 단순한 stub."""

    def __init__(self, fixed_cost: Decimal) -> None:
        if fixed_cost < Decimal("0"):
            raise ValueError("fixed_cost must be >= 0")
        self._fixed_cost = fixed_cost

    def estimate(self, signal: Signal) -> Decimal:
        from src.signals.schema import SignalAction
        if signal.action in (SignalAction.SELL, SignalAction.CLOSE, SignalAction.HOLD):
            return Decimal("0")
        return self._fixed_cost


# ============================================================
# 3. resolve_capital_conflict
# ============================================================

def resolve_capital_conflict(
    sorted_signals: Iterable[Signal],
    available_capital: Decimal,
    estimator: CapitalEstimator,
) -> tuple[tuple[Signal, ...], tuple[RejectedSignal, ...], Decimal]:
    """Stage 5: PRIORITY_THEN_FCFS 자본 컷.

    Args:
        sorted_signals: Stage 4 에서 (priority, as_of_utc) 정렬된 시그널.
        available_capital: 시작 자본 (>= 0).
        estimator: CapitalEstimator 구현체.

    Returns:
        (accepted, rejected, capital_consumed)
        capital_consumed = sum(estimate(s) for s in accepted)

    Raises:
        ValueError: available_capital < 0.

    Note:
        sorted_signals 의 정렬 순서를 *신뢰함* — 본 함수는 재정렬 안 함.
        호출자(runner.py)가 _sort 단계 후 호출해야 함.
    """
    if available_capital < Decimal("0"):
        raise ValueError(f"available_capital must be >= 0 (got {available_capital})")

    remaining = available_capital
    capital_consumed = Decimal("0")
    accepted: list[Signal] = []
    rejected: list[RejectedSignal] = []

    for s in sorted_signals:
        cost = estimator.estimate(s)
        if cost < Decimal("0"):
            raise ValueError(f"estimator returned negative cost {cost} for {s.signal_id}")

        if cost <= remaining:
            accepted.append(s)
            remaining -= cost
            capital_consumed += cost
        else:
            rejected.append(
                RejectedSignal(
                    signal=s,
                    reason=RejectionReason.LOWER_PRIORITY,
                    stage=5,
                    metadata={
                        "available_capital_at_decision": str(remaining),
                        "estimated_cost": str(cost),
                        "shortfall": str(cost - remaining),
                        "priority": s.priority(),
                        "category": s.signal_category.value,
                    },
                )
            )

    return tuple(accepted), tuple(rejected), capital_consumed


__all__ = [
    "CapitalEstimator",
    "FixedCapitalEstimator",
    "StubCapitalEstimator",
    "resolve_capital_conflict",
]
