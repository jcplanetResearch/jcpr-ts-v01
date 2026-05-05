"""src/signals/_conflict.py — Task 16 Stage 3 v0.2: Conflict (R1 명세).

책임 (Responsibilities)
-----------------------
- R4: per-symbol direction conflict 검출 — **동일 카테고리 내에서만**

v0.2 변경 (Changes from v0.1)
-----------------------------
- v0.1: (symbol, direction) 그룹화 → 카테고리 무시 → STOP_LOSS 도 함께 거부
- v0.2: (symbol, signal_category, direction) 그룹화 → 동일 카테고리 내 충돌만 거부
- 교차 카테고리 충돌은 Stage 4 (sort + supersession) 에서 우선순위로 해결

설계 근거 (§3.2 7가지 케이스)
-----------------------------
| 케이스                                              | Stage 3 처리                  |
|-----------------------------------------------------|-------------------------------|
| 동일 symbol + 동일 category + BUY+SELL              | 양쪽 REJECT (CONFLICTING)     |
| 동일 symbol + 동일 category + BUY+CLOSE             | 양쪽 REJECT (CONFLICTING)     |
| 동일 symbol + 다른 category + STOP_LOSS-SELL+ENTRY-BUY | **통과** (Stage 4 에서 처리)  |
| 동일 symbol + 다른 category + EXIT-SELL+ENTRY-BUY   | **통과** (Stage 4 에서 처리)  |
| 동일 symbol + 다른 category + 동방향 (REBALANCE+ENTRY) | **통과** (둘 다 OK)           |
| 다른 symbol                                          | **통과**                      |
| HOLD only                                            | **통과** (HOLD 충돌 대상 아님) |

Long-only assumption
--------------------
direction = BUY | SELL_OR_CLOSE. SHORT 도입 시 SHORT 방향 추가.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from src.risk import RejectionReason
from src.signals._decision import RejectedSignal
from src.signals.schema import Signal, SignalAction, SignalCategory


# ============================================================
# 헬퍼: SignalAction → direction 그룹
# ============================================================

def _direction(action: SignalAction) -> str:
    """SignalAction → direction (BUY | SELL_OR_CLOSE | NONE).
    
    HOLD 는 NONE — 충돌 대상 아님 (의사결정 미발생).
    """
    if action == SignalAction.BUY:
        return "BUY"
    if action in (SignalAction.SELL, SignalAction.CLOSE):
        return "SELL_OR_CLOSE"
    return "NONE"


# ============================================================
# detect_conflicts v0.2
# ============================================================

def detect_conflicts(
    signals: Iterable[Signal],
) -> tuple[tuple[Signal, ...], tuple[RejectedSignal, ...]]:
    """Stage 3 v0.2: 동일 (symbol, signal_category) 그룹 내 방향 충돌만 거부.
    
    Args
    ----
    signals : Iterable[Signal]
        Stage 2 통과 시그널.
    
    Returns
    -------
    (accepted, rejected)
        - accepted : 입력 순서 보존, 충돌 없는 시그널
        - rejected : 동일 카테고리 내 방향 충돌 시그널 (CONFLICTING_SIGNALS)
    
    Algorithm
    ---------
    1. (symbol, signal_category) 키로 그룹화
    2. 각 그룹의 actionable_directions 집합 (HOLD 제외) 계산
    3. |actionable_directions| >= 2 → 그룹 전체 REJECT (CONFLICTING_SIGNALS)
    4. 그렇지 않으면 그룹 전체 통과
    
    교차 카테고리 충돌은 Stage 3 에서 검출 안 함 — Stage 4 supersession 책임.
    
    예시:
        입력: [
            STOP_LOSS-SELL(005930),  # group A
            ENTRY-BUY(005930),        # group B (다른 카테고리 → 다른 그룹)
        ]
        Stage 3 결과: 양쪽 모두 통과 (그룹 A, B 각각 단일 방향)
        Stage 4 에서 STOP_LOSS 통과 + ENTRY supersession 처리.
        
        입력: [
            ENTRY-BUY(005930),
            ENTRY-SELL(005930),  # 동일 카테고리 + 반대 방향
        ]
        Stage 3 결과: 양쪽 모두 REJECT (CONFLICTING_SIGNALS)
    """
    input_signals = list(signals)
    
    # (symbol, category) 그룹화
    by_key: dict[tuple[str, SignalCategory], list[Signal]] = defaultdict(list)
    for s in input_signals:
        by_key[(s.symbol, s.signal_category)].append(s)
    
    # 거부될 signal_id 집합 — 그룹 단위 결정
    rejected_ids: set[str] = set()
    rejection_metadata: dict[str, dict] = {}
    
    for (symbol, category), group in by_key.items():
        directions = {_direction(s.action) for s in group}
        actionable_dirs = directions - {"NONE"}
        
        if len(actionable_dirs) >= 2:
            # 동일 카테고리 내 방향 충돌 — 그룹 전체 거부
            for s in group:
                rejected_ids.add(s.signal_id)
                rejection_metadata[s.signal_id] = {
                    "symbol": symbol,
                    "category": category.value,
                    "conflicting_directions": sorted(actionable_dirs),
                    "group_size": len(group),
                    "conflicting_signal_ids": sorted(
                        other.signal_id
                        for other in group
                        if other.signal_id != s.signal_id
                    ),
                }
    
    # 입력 순서 보존하며 분류
    accepted: list[Signal] = []
    rejected: list[RejectedSignal] = []
    
    for s in input_signals:
        if s.signal_id in rejected_ids:
            rejected.append(
                RejectedSignal(
                    signal=s,
                    reason=RejectionReason.CONFLICTING_SIGNALS,
                    stage=3,
                    metadata=rejection_metadata[s.signal_id],
                )
            )
        else:
            accepted.append(s)
    
    return tuple(accepted), tuple(rejected)


__all__ = ["detect_conflicts"]
