"""src.signals — 시그널 패키지 (Signal package).

Modules
-------
- schema         : Signal, SignalBatch, SignalCategory (Task 15 v0.2)
- strategies     : 구체 전략 (Task 14 v0.3 — momentum_v1)
- _runner_state  : StopState, CadenceTracker (Task 16 Stage 0)
- _decision      : RunnerDecision, RejectedSignal (Task 16 Stage 6)
- _filter        : Stage 1 filter
- _dedup         : Stage 2 dedup
- _conflict      : Stage 3 conflict
- _resolve       : Stage 5 capital resolution + CapitalEstimator
- runner         : SignalRunner orchestrator (Task 16 main)
"""
from src.signals._decision import (
    RejectedSignal,
    RunnerDecision,
    StageMetrics,
)
from src.signals._resolve import (
    CapitalEstimator,
    FixedCapitalEstimator,
    StubCapitalEstimator,
)
from src.signals._runner_state import (
    CadenceResult,
    CadenceTracker,
    RunnerStopReason,
    StopState,
    preflight_stop_check,
)
from src.signals.runner import RUNNER_VERSION, SignalRunner
from src.signals.schema import (
    MINIMUM_SIGNAL_CYCLE_SECONDS,
    Signal,
    SignalAction,
    SignalBatch,
    SignalCategory,
    SignalStrength,
)

__all__ = [
    # schema (Task 15)
    "MINIMUM_SIGNAL_CYCLE_SECONDS",
    "Signal",
    "SignalAction",
    "SignalBatch",
    "SignalCategory",
    "SignalStrength",
    # runner state (Task 16 Stage 0)
    "CadenceResult",
    "CadenceTracker",
    "RunnerStopReason",
    "StopState",
    "preflight_stop_check",
    # decision (Task 16 Stage 6)
    "RejectedSignal",
    "RunnerDecision",
    "StageMetrics",
    # resolve (Task 16 Stage 5)
    "CapitalEstimator",
    "FixedCapitalEstimator",
    "StubCapitalEstimator",
    # runner (Task 16 main)
    "RUNNER_VERSION",
    "SignalRunner",
]
