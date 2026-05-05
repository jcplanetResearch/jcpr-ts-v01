"""src/signals — 시그널 패키지 (Signal package).

v0.2 변경:
- SignalCategory export 추가
- MINIMUM_SIGNAL_CYCLE_SECONDS export 추가

Modules
-------
- schema     : 시그널 표준 스키마 (Task 15)
- strategies : 구체 전략 구현체 (Task 14 ~)
- runner     : 시그널 러너 (Task 16)
"""
from src.signals.schema import (
    MINIMUM_SIGNAL_CYCLE_SECONDS,
    Signal,
    SignalAction,
    SignalBatch,
    SignalCategory,
    SignalStrength,
)

__all__ = [
    "MINIMUM_SIGNAL_CYCLE_SECONDS",
    "Signal",
    "SignalAction",
    "SignalBatch",
    "SignalCategory",
    "SignalStrength",
]
