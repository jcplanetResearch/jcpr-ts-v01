"""src/signals/_filter.py — Task 16 Stage 1: Filter.

책임:
- R2: schema validation (이미 Pydantic Signal 인스턴스이므로 유효 — 추가 invariant 검사)
- R1: expired-signal filter (signal.is_expired(now))

설계:
- per-signal 독립 처리 (한 시그널 거부가 다른 시그널 처리에 영향 없음)
- 거부 시그널은 RejectedSignal 로 변환 (stage=1)
- 통과 시그널은 그대로 전달
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable

from src.risk import RejectionReason
from src.signals._decision import RejectedSignal
from src.signals.schema import Signal


def filter_signals(
    signals: Iterable[Signal],
    now_utc: datetime,
) -> tuple[tuple[Signal, ...], tuple[RejectedSignal, ...]]:
    """Stage 1: validation + expired filter.

    Args:
        signals: 입력 시그널 (이미 Pydantic 검증 통과한 Signal 인스턴스)
        now_utc: 현재 시각 (UTC tz-aware)

    Returns:
        (accepted, rejected) 튜플.

    Note:
        Pydantic Signal 모델은 인스턴스화 시점에 이미 검증됨.
        Stage 1 의 추가 검사는 *runtime invariant* — 시점 의존 검사:
        - is_expired(now) 검사
    """
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be tz-aware UTC")

    accepted: list[Signal] = []
    rejected: list[RejectedSignal] = []

    for s in signals:
        # R1: expired filter
        if s.is_expired(now_utc):
            rejected.append(
                RejectedSignal(
                    signal=s,
                    reason=RejectionReason.SIGNAL_EXPIRED,
                    stage=1,
                    metadata={
                        "now_utc": now_utc.isoformat(),
                        "expires_at_utc": s.expires_at_utc.isoformat() if s.expires_at_utc else None,
                    },
                )
            )
            continue

        # R2: schema-level invariant (Pydantic 보강)
        # 본 시점에서는 이미 Pydantic 검증 통과. 추가 invariant 없음.
        # 미래에 추가 검사 (예: symbol 화이트리스트) 시 여기에 위치.

        accepted.append(s)

    return tuple(accepted), tuple(rejected)


__all__ = ["filter_signals"]
