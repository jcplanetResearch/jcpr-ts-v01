"""파일 기반 Kill Switch — Task 31.

특정 경로(.env: KILL_SWITCH_PATH) 에 파일이 존재하면 신규 주문 영구 차단.
세션 시작 시점뿐 아니라 매 주문 발주 직전(check_now)에 검사된다.

설계 원칙 (Design):
1. Fail-safe — 파일 시스템 검사 실패 시 정지로 간주 (안전 측 가정)
2. Hot-path 호출 가능 — check_now() 가 매 주문 직전 호출되도록 빠르게 동작
3. 백그라운드 폴링 + 즉시 검사 이중 보장
4. 한 번 발동되면 세션 동안 영구 — kill switch 파일을 지워도 StopState
   는 immutable-once-set 이므로 정지 상태 유지

관련 모듈 (Related):
- src/risk/_state.py            — StopState 공유
- configs/risk_limits.example.yaml §9.1 kill_switch.check_before_every_order: true
- docs/00_target_operating_model.md §6.2 정지 시 동작 순서
"""
from __future__ import annotations
import threading
import time
from pathlib import Path
from typing import Optional

from ._state import StopState, StopReason, get_stop_state


class KillSwitchMonitor:
    """주기적·즉시 검사 가능한 kill switch 모니터.

    Args:
        path:              kill switch 파일 경로. 존재하면 정지 발동.
        state:             사용할 StopState. None 이면 전역 싱글톤.
        check_interval_s:  백그라운드 폴링 주기 (기본 1.0 초).

    Usage:
        monitor = KillSwitchMonitor("./runtime/KILL_SWITCH_ON")
        monitor.start()                # 백그라운드 시작 (선택)
        if not monitor.check_now():    # 주문 발주 직전 즉시 재검사
            place_order(intent)
    """

    DEFAULT_INTERVAL_S = 1.0

    def __init__(
        self,
        path: str | Path,
        state: Optional[StopState] = None,
        *,
        check_interval_s: float = DEFAULT_INTERVAL_S,
    ) -> None:
        if check_interval_s <= 0:
            raise ValueError("check_interval_s must be positive")
        self._path = Path(path)
        self._state = state or get_stop_state()
        self._interval = check_interval_s
        self._thread: Optional[threading.Thread] = None
        self._running = False

    # ------------------------------------------------------------
    # 즉시 검사 (Hot-path)
    # ------------------------------------------------------------
    def check_now(self) -> bool:
        """즉시 1회 검사.

        Returns:
            True  — 정지 발동되었거나 이미 stopped 상태
            False — kill switch 파일 부재, 정상 운영 가능

        Fail-safe:
            파일 시스템 검사 자체가 실패(OSError, PermissionError)하면
            정지로 간주하고 True 반환. 검사 실패는 시스템 이상의 신호.

        Hot-path:
            본 메서드는 매 주문 발주 직전 호출되도록 가볍게 구현됨.
            risk_limits.yaml §9.1 check_before_every_order: true 정합.
        """
        # 이미 stopped 면 즉시 True
        if self._state.is_stopped():
            return True

        # 파일 존재 확인 (fail-safe)
        try:
            present = self._path.exists()
        except OSError as e:
            # 파일 시스템 검사 실패 — fail-safe 정지
            self._state.request_stop(
                reason=StopReason.KILL_SWITCH,
                note=f"check failed (OSError): {type(e).__name__} — fail-safe stop",
            )
            return True

        if present:
            # 파일 자체의 내용은 읽지 않는다 (잠재적 시크릿 노출 방지)
            self._state.request_stop(
                reason=StopReason.KILL_SWITCH,
                note=f"file present: {self._path}",
            )
            return True

        return False

    # ------------------------------------------------------------
    # 라이프사이클 (Lifecycle)
    # ------------------------------------------------------------
    def start(self) -> None:
        """백그라운드 폴링 시작. 이미 실행 중이면 무시."""
        if self._thread is not None and self._thread.is_alive():
            return
        # 시작 시 1회 즉시 검사 (이미 파일이 있으면 즉시 정지)
        if self.check_now():
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._loop,
            name="kill-switch-monitor",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """폴링 종료 요청. 다음 사이클에서 종료."""
        self._running = False

    def is_alive(self) -> bool:
        """모니터 스레드가 실행 중인가?"""
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------
    # 내부: 폴링 루프
    # ------------------------------------------------------------
    def _loop(self) -> None:
        while self._running and not self._state.is_stopped():
            if self.check_now():
                return
            time.sleep(self._interval)

    # ------------------------------------------------------------
    # repr (시크릿 미포함)
    # ------------------------------------------------------------
    def __repr__(self) -> str:
        return (
            f"<KillSwitchMonitor path={self._path} "
            f"interval={self._interval}s "
            f"running={self._running}>"
        )
