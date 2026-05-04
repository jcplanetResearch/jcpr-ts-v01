"""StopState — 비상 정지 공유 상태 (Shared emergency-stop state).

세 가지 정지 입력(SIGINT, ESC, kill switch)이 모두 단일 인스턴스에 작용한다.
한 번 set 된 플래그는 세션 내내 절대 unset 되지 않는다 (immutable-once-set).

설계 원칙 (Design principles):
1. 정지 우선 (Stop-first) — <model> 의 절대 원칙
2. Immutable-once-set — 첫 정지 이벤트만 보존, 후속 요청 무시
3. Lock-free 검사 — is_stopped() 가 hot-path 부담 없이 호출 가능
4. Hook 실행 격리 — hook 의 예외가 정지 자체를 방해하지 않음
5. Async-signal-safe 친화 — 시그널 핸들러에서 호출 가능

관련 모듈 (Related):
- src/risk/shutdown.py        — Task 29, SIGINT 핸들러
- src/risk/keyboard_stop.py   — Task 30, ESC 리스너
- src/risk/kill_switch.py     — Task 31, kill switch 모니터
- src/execution/execution_gateway.py — Task 21, is_stopped() 를 매 주문 직전 검사
"""
from __future__ import annotations
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Optional


# ============================================================
# 1. 정지 사유 (Stop reason)
# ============================================================
class StopReason(str, Enum):
    """정지 발동 사유.

    SIGINT       : Ctrl-C 시그널
    ESC_KEY      : 키보드 ESC 입력 (또는 q)
    KILL_SWITCH  : 파일 기반 kill switch 발동
    MANUAL       : API 직접 호출 (테스트 또는 다른 제어 채널)
    """
    SIGINT = "SIGINT"
    ESC_KEY = "ESC_KEY"
    KILL_SWITCH = "KILL_SWITCH"
    MANUAL = "MANUAL"


# ============================================================
# 2. 정지 이벤트 (Stop event)
# ============================================================
@dataclass(frozen=True)
class StopEvent:
    """첫 정지 요청을 기록하는 immutable 이벤트.

    Attributes:
        reason:        정지 사유.
        requested_at:  UTC tz-aware datetime.
        note:          추가 컨텍스트 (시크릿 미포함).
    """
    reason: StopReason
    requested_at: datetime
    note: Optional[str] = None


# ============================================================
# 3. StopState (전역 공유 상태)
# ============================================================
class StopState:
    """전역 정지 상태.

    스레드 안전. is_stopped() 는 lock-free O(1) — threading.Event 사용.
    request_stop() 은 한 번만 effective; 후속 호출은 무시.
    """

    def __init__(self) -> None:
        self._stopped_event = threading.Event()
        self._first_event: Optional[StopEvent] = None
        self._lock = threading.Lock()
        self._hooks: list[Callable[[StopEvent], None]] = []

    # ------------------------------------------------------------
    # 상태 조회 (Hot-path: lock-free)
    # ------------------------------------------------------------
    def is_stopped(self) -> bool:
        """현재 정지 상태인가? hot-path 호출 가능 (lock-free)."""
        return self._stopped_event.is_set()

    def first_event(self) -> Optional[StopEvent]:
        """첫 정지 이벤트. 정지 전이면 None."""
        return self._first_event

    # ------------------------------------------------------------
    # 정지 요청
    # ------------------------------------------------------------
    def request_stop(
        self,
        reason: StopReason,
        note: Optional[str] = None,
    ) -> bool:
        """정지 요청. 처음 호출만 effective.

        Returns:
            True  — 본 호출이 정지를 처음 발동시켰음
            False — 이미 정지된 상태 (후속 호출은 무시)

        Hook 실행:
            등록된 hook 들이 순서대로 호출된다. hook 내부 예외는 격리되어
            다른 hook 실행을 방해하지 않는다 (정지 자체는 이미 발동된 상태).
        """
        with self._lock:
            if self._stopped_event.is_set():
                return False
            ev = StopEvent(
                reason=reason,
                requested_at=datetime.now(timezone.utc),
                note=note,
            )
            self._first_event = ev
            self._stopped_event.set()
            hooks_snapshot = list(self._hooks)  # lock 안에서 스냅샷

        # hooks 는 lock 밖에서 실행 (재진입 방지)
        for hook in hooks_snapshot:
            try:
                hook(ev)
            except Exception:
                # hook 실패는 정지 발동 자체를 방해하지 않음
                # (로깅은 호출자 책임)
                pass

        return True

    # ------------------------------------------------------------
    # Hook 관리
    # ------------------------------------------------------------
    def register_hook(self, hook: Callable[[StopEvent], None]) -> None:
        """정지 발동 시 호출될 hook 등록.

        Hook 은 짧고 빠르게 동작해야 한다 (Task 1 §6.2):
          1. 신규 주문 차단 (이미 StopState 가 처리)
          2. 진행 중 주문 취소
          3. 포지션 스냅샷
          4. 브로커 연결 종료
          5. 로그 flush
        """
        with self._lock:
            self._hooks.append(hook)

    def hook_count(self) -> int:
        """등록된 hook 개수 (테스트·디버깅용)."""
        with self._lock:
            return len(self._hooks)

    # ------------------------------------------------------------
    # 대기 (메인 스레드용)
    # ------------------------------------------------------------
    def wait(self, timeout: Optional[float] = None) -> bool:
        """정지될 때까지 블로킹 대기.

        Args:
            timeout: 최대 대기 초. None 이면 무한 대기.

        Returns:
            True  — 정지됨
            False — 타임아웃
        """
        return self._stopped_event.wait(timeout)


# ============================================================
# 4. 전역 싱글톤
# ============================================================
_GLOBAL_STOP_STATE = StopState()


def get_stop_state() -> StopState:
    """전역 StopState 싱글톤 반환.

    시스템 전체에서 동일 인스턴스를 공유한다. 테스트에서는 별도 StopState()
    를 인자로 전달하여 격리할 수 있다.
    """
    return _GLOBAL_STOP_STATE


def _reset_global_for_testing() -> None:
    """테스트 전용: 전역 싱글톤을 새 인스턴스로 교체.

    프로덕션 코드에서 호출 금지. 테스트 격리를 위해서만 사용.
    """
    global _GLOBAL_STOP_STATE
    _GLOBAL_STOP_STATE = StopState()
