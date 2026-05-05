"""src/signals/runner.py — Task 16 v0.2: SignalRunner 오케스트레이터.

Stage 0~6 전체 파이프라인 조율.

v0.2 변경 (Changes from v0.1)
-----------------------------
- Stage 4 (Sort) 에 cross-category supersession 로직 추가 (R1 명세 §3.2)
- _conflict.py v0.2 (동일 카테고리 충돌만 거부) 와 짝을 이룸
- supersession metadata에 superseded_by_signal_id, superseded_by_category 명시

설계
----
- SignalRunner.run_cycle(batch, available_capital, now_utc, cycle_id) → RunnerDecision
- stateless 가까운 함수형 + CadenceTracker 만 인스턴스 상태
- 모든 외부 의존(StopState, CapitalEstimator)은 생성자에서 주입

흐름
----
    Stage 0  Pre-cycle  : cadence + stop preflight
    Stage 1  Filter     : expired + invariants
    Stage 2  Dedup      : (inputs_hash, signal_category)
    Stage 3  Conflict   : per-(symbol, category) direction conflict
    Stage 4  Sort       : HOLD 제거 + cross-category supersession + priority sort
    Stage 5  Resolve    : capital cut (PRIORITY_THEN_FCFS)
    Stage 6  Emit       : RunnerDecision frozen

비상 정지 시: Stage 1~5 건너뛰고 즉시 RunnerDecision 반환 — 모든 입력
시그널은 EMERGENCY_STOP_ACTIVE / KILL_SWITCH_ACTIVE / KEYBOARD_STOP_ACTIVE
사유로 stage=0 거부.

Cadence 위반 시: 시그널은 거부 안 함 (stage=0 reject 미발행).
RunnerDecision.cadence_violation=True + accepted_signals=()  반환.
다음 호출에서 동일 batch 재처리 가능.

Cross-category supersession (Stage 4 v0.2)
------------------------------------------
동일 symbol 에 다른 카테고리의 반대 방향 시그널이 공존할 때:
- 우선순위 높은 카테고리만 통과
- 우선순위 낮은 카테고리는 LOWER_PRIORITY 사유로 거부 (stage=4)
- metadata에 superseded_by_signal_id, superseded_by_category 명시

같은 방향이면 supersession 없음 (둘 다 통과 — Stage 5 자본 컷에서 처리).

Audit
-----
본 모듈은 RunnerDecision 산출까지. 영속 audit 저장은 Task 9 책임.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from src.risk import RejectionReason
from src.signals._conflict import detect_conflicts
from src.signals._decision import RejectedSignal, RunnerDecision, StageMetrics
from src.signals._dedup import dedup_signals
from src.signals._filter import filter_signals
from src.signals._resolve import (
    CapitalEstimator,
    resolve_capital_conflict,
)
from src.signals._runner_state import (
    CadenceTracker,
    RunnerStopReason,
    StopState,
    preflight_stop_check,
)
from src.signals.schema import Signal, SignalAction, SignalBatch


RUNNER_VERSION = "v1.0.0-stage4-supersession"


# Stage 0 의 stop 사유별 RejectionReason 매핑
_STOP_REASON_TO_REJECTION = {
    RunnerStopReason.KILL_SWITCH: RejectionReason.KILL_SWITCH_ACTIVE,
    RunnerStopReason.EMERGENCY_STOP: RejectionReason.EMERGENCY_STOP_ACTIVE,
    RunnerStopReason.KEYBOARD_STOP: RejectionReason.KEYBOARD_STOP_ACTIVE,
}


def _signal_direction(s: Signal) -> str:
    """Signal → direction 그룹 (BUY | SELL_OR_CLOSE | NONE)."""
    if s.action == SignalAction.BUY:
        return "BUY"
    if s.action in (SignalAction.SELL, SignalAction.CLOSE):
        return "SELL_OR_CLOSE"
    return "NONE"


def _apply_cross_category_supersession(
    signals: tuple[Signal, ...],
) -> tuple[tuple[Signal, ...], tuple[RejectedSignal, ...]]:
    """Stage 4 cross-category supersession (R1 명세 §3.2).
    
    동일 symbol 내 다른 카테고리의 반대 방향 시그널이 공존하면:
    - 우선순위 높은 카테고리만 통과
    - 우선순위 낮은 카테고리는 LOWER_PRIORITY 사유로 stage=4 거부
    
    동일 symbol + 같은 방향 (다른 카테고리) → 둘 다 통과 (Stage 5 자본 컷에서 처리)
    
    Algorithm
    ---------
    1. symbol 별 그룹화
    2. 각 그룹의 directions 집합 (HOLD 제외) 계산
    3. directions 가 단일 방향이면 → 모두 통과 (자본 컷에서 처리)
    4. directions 가 양방향이면 →
       - 최고 우선순위 카테고리 식별 (priority 최소값)
       - 그 카테고리의 시그널만 통과
       - 나머지 시그널은 LOWER_PRIORITY 거부
       - metadata: superseded_by_signal_id, superseded_by_category, superseding_priority
    
    참고: HOLD 시그널은 본 함수 호출 전 이미 제거되어야 함 (호출자 책임).
    """
    accepted: list[Signal] = []
    rejected: list[RejectedSignal] = []
    
    # symbol 별 그룹화
    by_symbol: dict[str, list[Signal]] = {}
    for s in signals:
        by_symbol.setdefault(s.symbol, []).append(s)
    
    for symbol, group in by_symbol.items():
        directions = {_signal_direction(s) for s in group}
        actionable_dirs = directions - {"NONE"}
        
        if len(actionable_dirs) <= 1:
            # 단일 방향 (또는 비어있음) — 모두 통과
            accepted.extend(group)
            continue
        
        # 양방향 공존 — supersession 적용
        # 최고 우선순위(priority 최소) 카테고리의 시그널 식별
        priorities = sorted({s.signal_category.priority() for s in group})
        winning_priority = priorities[0]
        
        # 같은 priority 내 동률 처리: as_of_utc 빠른 순 + signal_id tie-break
        winners = sorted(
            [s for s in group if s.signal_category.priority() == winning_priority],
            key=lambda s: (s.as_of_utc, s.signal_id),
        )
        
        # 승자 그룹 내에서도 카테고리 다양성 가능 (예: STOP_LOSS + RISK_REDUCE 모두 priority 1)
        # 승자 그룹의 방향 집합 검사 — 양방향이면 동일 카테고리 충돌 (이미 Stage 3에서 처리됐어야 함)
        # 그러나 다른 카테고리가 같은 priority 라면 Stage 3 검출 못함 — 추가 안전망 필요
        winner_dirs = {_signal_direction(s) for s in winners} - {"NONE"}
        if len(winner_dirs) >= 2:
            # 동일 priority 다른 카테고리 양방향 — 안전을 위해 모두 거부 (fail-closed)
            # (예: STOP_LOSS-SELL + RISK_REDUCE-BUY 동시 발생 — 매우 이례적)
            for s in winners:
                rejected.append(
                    RejectedSignal(
                        signal=s,
                        reason=RejectionReason.CONFLICTING_SIGNALS,
                        stage=4,
                        metadata={
                            "symbol": symbol,
                            "case": "same_priority_cross_category_conflict",
                            "priority": winning_priority,
                            "directions": sorted(winner_dirs),
                            "categories": sorted({s.signal_category.value for s in winners}),
                        },
                    )
                )
            # 승자 그룹 내 충돌 → loser 들도 함께 거부 (fail-closed)
            losers = [s for s in group if s not in winners]
            for s in losers:
                rejected.append(
                    RejectedSignal(
                        signal=s,
                        reason=RejectionReason.LOWER_PRIORITY,
                        stage=4,
                        metadata={
                            "symbol": symbol,
                            "case": "superseded_but_winners_conflicted",
                            "priority": s.signal_category.priority(),
                            "category": s.signal_category.value,
                        },
                    )
                )
            continue
        
        # 정상 supersession — 승자 통과, 패자 LOWER_PRIORITY
        accepted.extend(winners)
        # 대표 superseder = winners 중 첫 번째 (FCFS)
        superseder = winners[0]
        for s in group:
            if s in winners:
                continue
            rejected.append(
                RejectedSignal(
                    signal=s,
                    reason=RejectionReason.LOWER_PRIORITY,
                    stage=4,
                    metadata={
                        "symbol": symbol,
                        "case": "superseded_by_higher_priority_category",
                        "this_priority": s.signal_category.priority(),
                        "this_category": s.signal_category.value,
                        "superseder_signal_id": superseder.signal_id,
                        "superseder_category": superseder.signal_category.value,
                        "superseder_priority": winning_priority,
                    },
                )
            )
    
    return tuple(accepted), tuple(rejected)


# ============================================================
# SignalRunner
# ============================================================

class SignalRunner:
    """시그널 러너 — Stage 0~6 오케스트레이터."""
    
    def __init__(
        self,
        stop_state: StopState,
        capital_estimator: CapitalEstimator,
        cadence_tracker: Optional[CadenceTracker] = None,
    ) -> None:
        if not isinstance(stop_state, StopState):
            raise TypeError("stop_state must implement StopState protocol")
        if not isinstance(capital_estimator, CapitalEstimator):
            raise TypeError("capital_estimator must implement CapitalEstimator protocol")
        
        self._stop_state = stop_state
        self._capital_estimator = capital_estimator
        self._cadence = cadence_tracker if cadence_tracker is not None else CadenceTracker()
    
    @property
    def cadence_tracker(self) -> CadenceTracker:
        return self._cadence
    
    def run_cycle(
        self,
        batch: SignalBatch,
        available_capital: Decimal,
        now_utc: datetime,
        cycle_id: Optional[str] = None,
    ) -> RunnerDecision:
        """단일 사이클 실행.
        
        Args
        ----
        batch : SignalBatch
            입력 SignalBatch.
        available_capital : Decimal
            시작 자본 (>= 0).
        now_utc : datetime
            현재 시각 (tz-aware UTC).
        cycle_id : Optional[str]
            사이클 식별자. None 이면 batch.batch_id 사용.
        
        Returns
        -------
        RunnerDecision (frozen).
        """
        # 입력 검증
        if now_utc.tzinfo is None:
            raise ValueError("now_utc must be tz-aware UTC")
        if available_capital < Decimal("0"):
            raise ValueError("available_capital must be >= 0")
        if not isinstance(batch, SignalBatch):
            raise TypeError("batch must be a SignalBatch")
        
        cid = cycle_id if cycle_id else batch.batch_id
        
        # ========================================================
        # Stage 0a — Cadence
        # ========================================================
        cadence_result = self._cadence.check_and_advance(now_utc)
        if not cadence_result.allowed:
            return RunnerDecision(
                cycle_id=cid,
                runner_version=RUNNER_VERSION,
                as_of_utc=now_utc,
                input_batch_id=batch.batch_id,
                input_strategy_name=batch.strategy_name,
                input_strategy_version=batch.strategy_version,
                accepted_signals=(),
                rejected_signals=(),  # 시그널 거부 안 함 — 재시도 가능
                stage_metrics=StageMetrics(),
                cadence_violation=True,
                cadence_elapsed_seconds=cadence_result.elapsed_seconds,
                cadence_next_allowed_at=cadence_result.next_allowed_at,
                available_capital_at_start=available_capital,
            )
        
        # ========================================================
        # Stage 0b — Stop preflight
        # ========================================================
        stop_reason = preflight_stop_check(self._stop_state)
        if stop_reason is not None:
            rejection_reason = _STOP_REASON_TO_REJECTION[stop_reason]
            stop_rejected = tuple(
                RejectedSignal(
                    signal=s,
                    reason=rejection_reason,
                    stage=0,
                    metadata={"stop_reason": stop_reason.value},
                )
                for s in batch.signals
            )
            return RunnerDecision(
                cycle_id=cid,
                runner_version=RUNNER_VERSION,
                as_of_utc=now_utc,
                input_batch_id=batch.batch_id,
                input_strategy_name=batch.strategy_name,
                input_strategy_version=batch.strategy_version,
                accepted_signals=(),
                rejected_signals=stop_rejected,
                stage_metrics=StageMetrics(),
                stop_engaged=True,
                stop_reason=stop_reason,
                available_capital_at_start=available_capital,
            )
        
        # ========================================================
        # Stage 1 — Filter
        # ========================================================
        all_rejected: list[RejectedSignal] = []
        s1_in = len(batch.signals)
        s1_acc, s1_rej = filter_signals(batch.signals, now_utc)
        all_rejected.extend(s1_rej)
        s1_out = len(s1_acc)
        
        # ========================================================
        # Stage 2 — Dedup
        # ========================================================
        s2_in = s1_out
        s2_acc, s2_rej = dedup_signals(s1_acc)
        all_rejected.extend(s2_rej)
        s2_out = len(s2_acc)
        
        # ========================================================
        # Stage 3 — Conflict (v0.2: 동일 카테고리 충돌만)
        # ========================================================
        s3_in = s2_out
        s3_acc, s3_rej = detect_conflicts(s2_acc)
        all_rejected.extend(s3_rej)
        s3_out = len(s3_acc)
        
        # ========================================================
        # Stage 4 — Sort (v0.2: HOLD 제거 + supersession + priority sort)
        # ========================================================
        # Stage 4 의 3 단계 — 모두 metrics 상 한 단계로 집계:
        #   4a) HOLD 제거 — 거부 아님 (자본·체결 대상 외)
        #   4b) cross-category supersession (R1 §3.2) — LOWER_PRIORITY 거부
        #   4c) priority+as_of_utc 안정 정렬
        #
        # metrics 매핑:
        #   stage_4_sort_in  = HOLD 제거 후 actionable 갯수 (supersession 입력)
        #   stage_4_sort_out = supersession 후 + 정렬 후 갯수 (Stage 5 입력)
        # 이 두 수의 차이 = supersession 거부 갯수.
        # 단계 흐름 invariant 는 강제 안 됨 (s3_out 에는 HOLD 포함, s4_in 은 actionable 만).
        
        # 4a: HOLD 제거 (자본 소비 없고 OrderIntent 변환 대상 아님)
        actionable = tuple(s for s in s3_acc if s.is_actionable())
        s4_in_for_metrics = len(actionable)
        
        # 4b: cross-category supersession (R1 명세)
        post_supersession, s4_rej = _apply_cross_category_supersession(actionable)
        all_rejected.extend(s4_rej)
        
        # 4c: priority sort (안정 정렬 — Python sorted 는 stable)
        sorted_signals = tuple(
            sorted(
                post_supersession,
                key=lambda s: (s.priority(), s.as_of_utc, s.signal_id),
            )
        )
        s4_out = len(sorted_signals)
        
        # ========================================================
        # Stage 5 — Resolve (capital)
        # ========================================================
        s5_in = s4_out
        s5_acc, s5_rej, capital_consumed = resolve_capital_conflict(
            sorted_signals,
            available_capital,
            self._capital_estimator,
        )
        all_rejected.extend(s5_rej)
        s5_out = len(s5_acc)
        
        # ========================================================
        # Stage 6 — Emit
        # ========================================================
        # Note: stage_3_conflict_out 는 실제 conflict 통과 갯수
        # stage_4 는 HOLD 제거 + supersession 적용 후 = sort_in (StageMetrics 강제)
        metrics = StageMetrics(
            stage_1_filter_in=s1_in,
            stage_1_filter_out=s1_out,
            stage_2_dedup_in=s2_in,
            stage_2_dedup_out=s2_out,
            stage_3_conflict_in=s3_in,
            stage_3_conflict_out=s3_out,
            stage_4_sort_in=s4_in_for_metrics,
            stage_4_sort_out=s4_out,
            stage_5_resolve_in=s5_in,
            stage_5_resolve_out=s5_out,
        )
        
        return RunnerDecision(
            cycle_id=cid,
            runner_version=RUNNER_VERSION,
            as_of_utc=now_utc,
            input_batch_id=batch.batch_id,
            input_strategy_name=batch.strategy_name,
            input_strategy_version=batch.strategy_version,
            accepted_signals=s5_acc,
            rejected_signals=tuple(all_rejected),
            stage_metrics=metrics,
            available_capital_at_start=available_capital,
            capital_consumed_estimate=capital_consumed,
        )


__all__ = ["RUNNER_VERSION", "SignalRunner"]
