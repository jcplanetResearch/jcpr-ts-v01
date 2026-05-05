"""src/signals/_runner_state.py — Task 16: Runner State (Stage 0).

Stage 0 (Pre-cycle) 의 두 컴포넌트:
1. StopState 프로토콜 + RunnerStopReason
   - Task 29-31 의 비상 정지 상태 인터페이스
   - 우선순위: KILL_SWITCH > EMERGENCY_STOP > KEYBOARD_STOP
2. CadenceTracker
   - 사이클 빈도 강제 (≥ MINIMUM_SIGNAL_CYCLE_SECONDS = 5초)
   - 미준수 시 cadence_violation 표시 (RunnerDecision 에 반영)

Concurrency
-----------
CadenceTracker 는 단일 SignalRunner 인스턴스에 종속. MVP 는 sequential.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional, Protocol, runtime_checkable


# ============================================================
# 1. RunnerStopReason
# ============================================================

class RunnerStopReason(str, Enum):
    """비상 정지 사유 (Stage 0 preflight 결과)."""
    KILL_SWITCH = "KILL_SWITCH"
    EMERGENCY_STOP = "EMERGENCY_STOP"  # Ctrl-C
    KEYBOARD_STOP = "KEYBOARD_STOP"    # ESC


# ============================================================
# 2. StopState 프로토콜 (Task 29-31 인터페이스)
# ============================================================

@runtime_checkable
class StopState(Protocol):
    """비상 정지 상태 조회 프로토콜.

    구현체는 Task 29 (shutdown.py), Task 30 (keyboard_stop.py),
    Task 31 (kill_switch.py) 에서 제공.

    본 프로토콜은 read-only. 상태 변경은 각 모듈의 책임.
    """

    def kill_switch_active(self) -> bool: ...
    def emergency_stop_engaged(self) -> bool: ...
    def keyboard_stop_engaged(self) -> bool: ...


def preflight_stop_check(state: StopState) -> Optional[RunnerStopReason]:
    """Stage 0 비상 정지 검사 — 우선순위 보장.

    우선순위: KILL_SWITCH > EMERGENCY_STOP > KEYBOARD_STOP
    이는 영속성/심각성 순. 어느 하나라도 활성이면 첫 매칭만 반환.
    """
    if state.kill_switch_active():
        return RunnerStopReason.KILL_SWITCH
    if state.emergency_stop_engaged():
        return RunnerStopReason.EMERGENCY_STOP
    if state.keyboard_stop_engaged():
        return RunnerStopReason.KEYBOARD_STOP
    return None


# ============================================================
# 3. CadenceResult
# ============================================================

@dataclass(frozen=True)
class CadenceResult:
    """사이클 빈도 검사 결과."""
    allowed: bool
    elapsed_seconds: Optional[float]  # 첫 호출 시 None
    next_allowed_at: Optional[datetime] = None  # 거부 시 다음 허용 시각

    def __post_init__(self) -> None:
        if not self.allowed and self.next_allowed_at is None:
            raise ValueError("next_allowed_at required when not allowed")
        if self.next_allowed_at is not None and self.next_allowed_at.tzinfo is None:
            raise ValueError("next_allowed_at must be tz-aware")


# ============================================================
# 4. CadenceTracker
# ============================================================

class CadenceTracker:
    """사이클 빈도 강제 (≥ min_cycle_seconds).

    상태:
        _last_cycle_at : 최근 허용된 사이클 시각 (UTC tz-aware)
        _min_cycle_seconds : 최소 간격

    동작:
        - 첫 호출: 무조건 허용, 시각 기록
        - 이후: now - last < min → 거부
        - 시각 역행 (now < last): 거부 (시계 이상 의심)
    """

    DEFAULT_MIN_CYCLE_SECONDS = 5

    def __init__(self, min_cycle_seconds: int = DEFAULT_MIN_CYCLE_SECONDS) -> None:
        if min_cycle_seconds < 1:
            raise ValueError("min_cycle_seconds must be >= 1")
        self._min_cycle_seconds = min_cycle_seconds
        self._last_cycle_at: Optional[datetime] = None

    @property
    def min_cycle_seconds(self) -> int:
        return self._min_cycle_seconds

    @property
    def last_cycle_at(self) -> Optional[datetime]:
        return self._last_cycle_at

    def check_and_advance(self, now_utc: datetime) -> CadenceResult:
        """검사 + 통과 시 시각 갱신.

        Args:
            now_utc: 현재 시각 (UTC tz-aware).

        Returns:
            CadenceResult.allowed=True 시 _last_cycle_at 갱신됨.
            False 시 갱신 안 됨 (다음 호출에서 재시도 가능).
        """
        if now_utc.tzinfo is None:
            raise ValueError("now_utc must be tz-aware UTC")

        if self._last_cycle_at is None:
            self._last_cycle_at = now_utc
            return CadenceResult(allowed=True, elapsed_seconds=None)

        elapsed = (now_utc - self._last_cycle_at).total_seconds()

        # 시각 역행 거부 (시계 이상)
        if elapsed < 0:
            return CadenceResult(
                allowed=False,
                elapsed_seconds=elapsed,
                next_allowed_at=self._last_cycle_at + timedelta(seconds=self._min_cycle_seconds),
            )

        if elapsed < self._min_cycle_seconds:
            return CadenceResult(
                allowed=False,
                elapsed_seconds=elapsed,
                next_allowed_at=self._last_cycle_at + timedelta(seconds=self._min_cycle_seconds),
            )

        self._last_cycle_at = now_utc
        return CadenceResult(allowed=True, elapsed_seconds=elapsed)

    def reset(self) -> None:
        """테스트/세션 재시작용. 운영 중 호출 금지."""
        self._last_cycle_at = None


__all__ = [
    "CadenceResult",
    "CadenceTracker",
    "RunnerStopReason",
    "StopState",
    "preflight_stop_check",
]
