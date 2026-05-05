"""src/signals/_dedup.py — Task 16 Stage 2: Dedup.

책임:
- R3: dedup via (inputs_hash, signal_category) 복합키
  - 동일 키 시 as_of_utc 빠른 순(FCFS) 첫 번째만 보존
  - 나머지는 DUPLICATE_SIGNAL 사유로 거부

설계 근거:
- inputs_hash 만 사용 시 다른 카테고리(예: STOP_LOSS vs ENTRY) 가
  같은 hash 면 잘못 dedup 됨. 카테고리 추가로 의도 보존.
- 사이클 내 dedup만 (세션 dedup 아님 — 의도된 재시그널 보존)
"""
from __future__ import annotations

from typing import Iterable

from src.risk import RejectionReason
from src.signals._decision import RejectedSignal
from src.signals.schema import Signal, SignalCategory


def dedup_signals(
    signals: Iterable[Signal],
) -> tuple[tuple[Signal, ...], tuple[RejectedSignal, ...]]:
    """Stage 2: (inputs_hash, signal_category) 복합키로 dedup.

    Args:
        signals: Stage 1 통과 시그널.

    Returns:
        (accepted, rejected). accepted 는 입력 순서 보존 (FCFS).

    Algorithm:
        1. 입력을 as_of_utc 오름차순 정렬 (FCFS 결정성 확보)
        2. 키 (inputs_hash, signal_category) 첫 등장만 보존
        3. 나머지는 DUPLICATE_SIGNAL + metadata에 first_signal_id
    """
    # FCFS 결정성: as_of_utc 정렬 + signal_id (tie-break)
    sorted_signals = sorted(signals, key=lambda s: (s.as_of_utc, s.signal_id))

    seen: dict[tuple[str, SignalCategory], Signal] = {}
    accepted: list[Signal] = []
    rejected: list[RejectedSignal] = []

    for s in sorted_signals:
        key = (s.inputs_hash, s.signal_category)
        if key in seen:
            first = seen[key]
            rejected.append(
                RejectedSignal(
                    signal=s,
                    reason=RejectionReason.DUPLICATE_SIGNAL,
                    stage=2,
                    metadata={
                        "first_signal_id": first.signal_id,
                        "first_as_of_utc": first.as_of_utc.isoformat(),
                        "inputs_hash": s.inputs_hash,
                        "signal_category": s.signal_category.value,
                    },
                )
            )
        else:
            seen[key] = s
            accepted.append(s)

    return tuple(accepted), tuple(rejected)


__all__ = ["dedup_signals"]
