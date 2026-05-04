"""ESC 키 감지 — Task 30.

POSIX (Linux/macOS) 와 Windows 모두 표준 라이브러리만으로 stdin 을 폴링.
외부 의존성(keyboard, pynput) 없음.

Phase 1 단순화 (Phase 1 simplification):
  터미널 raw mode 진입은 환경 영향이 크므로 본 구현은 line-buffered mode
  에서 동작한다. 사용자가 다음 중 하나를 입력하고 Enter 를 눌러 정지 요청:
    - ESC (\x1b)
    - "esc" (대소문자 무관)
    - "q" (옵션, accept_q_as_quit=True 일 때, 기본값 True)

  추후 raw mode + termios 사용으로 단일 ESC 키스트로크에 즉각 반응하도록
  확장 가능. 환경 호환성 우려로 Phase 1 에서는 보수적으로 구현.

설계 원칙 (Design):
- 백그라운드 daemon thread — 메인 스레드는 차단되지 않음
- StopState 가 set 되면 자동 종료 (자체 polling 종료)
- 표준 라이브러리만 사용 — 외부 의존성 없음
- TTY 가 아니면 시작은 가능하나 의미 없는 대기 — start() 직후 stop() 가능

관련 모듈 (Related):
- src/risk/_state.py        — StopState 공유
- docs/00_target_operating_model.md §6 — 정지 우선 원칙
"""
from __future__ import annotations
import sys
import threading
import time
from typing import Optional

from ._state import StopState, StopReason, get_stop_state


# ASCII 27 (Escape character)
_ESC = "\x1b"


class EscKeyListener:
    """백그라운드에서 stdin 을 폴링하여 ESC 키 (또는 q) 감지.

    Args:
        state: 사용할 StopState. None 이면 전역 싱글톤.
        accept_q_as_quit: 'q' 입력도 정지 트리거로 인정할지 (기본 True).
        poll_interval_s: stdin 폴링 주기 (기본 0.1 초).
    """

    DEFAULT_POLL_INTERVAL_S = 0.1

    def __init__(
        self,
        state: Optional[StopState] = None,
        *,
        accept_q_as_quit: bool = True,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    ) -> None:
        if poll_interval_s <= 0:
            raise ValueError("poll_interval_s must be positive")
        self._state = state or get_stop_state()
        self._accept_q = accept_q_as_quit
        self._poll = poll_interval_s
        self._thread: Optional[threading.Thread] = None
        self._running = False

    # ------------------------------------------------------------
    # 라이프사이클 (Lifecycle)
    # ------------------------------------------------------------
    def start(self) -> None:
        """백그라운드 리스너 시작. 이미 실행 중이면 무시."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._loop,
            name="esc-key-listener",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """리스너 종료 요청. 다음 폴링 사이클에서 종료."""
        self._running = False

    def is_alive(self) -> bool:
        """리스너 스레드가 실행 중인가?"""
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------
    # 내부: 폴링 루프
    # ------------------------------------------------------------
    def _loop(self) -> None:
        while self._running and not self._state.is_stopped():
            line = self._read_line_nonblocking(self._poll)
            if line is None:
                continue
            stripped = line.strip()
            if self._matches_stop_signal(stripped):
                self._state.request_stop(
                    reason=StopReason.ESC_KEY,
                    note=f"input={stripped!r}",
                )
                return  # 정지되면 루프 즉시 종료

    def _matches_stop_signal(self, line: str) -> bool:
        """입력 문자열이 정지 신호인가?"""
        if not line:
            return False
        if line == _ESC:
            return True
        if line.lower() == "esc":
            return True
        if self._accept_q and line.lower() == "q":
            return True
        return False

    # ------------------------------------------------------------
    # 플랫폼별 비차단 stdin 읽기
    # ------------------------------------------------------------
    @staticmethod
    def _read_line_nonblocking(timeout: float) -> Optional[str]:
        """timeout 초 안에 stdin 에서 한 줄을 읽거나 None 반환.

        POSIX: select 로 readable 검사 후 readline.
        Windows: msvcrt.kbhit 폴링.
        """
        if sys.platform != "win32":
            return EscKeyListener._read_line_posix(timeout)
        else:
            return EscKeyListener._read_line_windows(timeout)

    @staticmethod
    def _read_line_posix(timeout: float) -> Optional[str]:
        """POSIX: select 기반."""
        import select
        try:
            r, _, _ = select.select([sys.stdin], [], [], timeout)
        except (ValueError, OSError):
            # stdin 이 닫혔거나 사용 불가능
            time.sleep(timeout)
            return None
        if r:
            try:
                return sys.stdin.readline()
            except (OSError, ValueError):
                return None
        return None

    @staticmethod
    def _read_line_windows(timeout: float) -> Optional[str]:
        """Windows: msvcrt 기반 폴링."""
        try:
            import msvcrt  # noqa: F401 — Windows 전용
        except ImportError:
            time.sleep(timeout)
            return None
        import msvcrt as _msvcrt
        elapsed = 0.0
        step = min(0.02, timeout)
        while elapsed < timeout:
            if _msvcrt.kbhit():
                try:
                    return sys.stdin.readline()
                except (OSError, ValueError):
                    return None
            time.sleep(step)
            elapsed += step
        return None
