"""Ctrl-C (SIGINT) 시그널 핸들링 — Task 29.

표준 signal 모듈 사용. 시그널 핸들러는 async-signal-safe 한 동작만 수행 —
즉 StopState 의 플래그만 set 하고 즉시 반환. 실제 cleanup 은 hook 또는
메인 스레드에서 수행한다.

설계 원칙 (Design):
- 시그널 핸들러는 단순하게 — request_stop 호출만
- 두 번째 SIGINT 입력 시 즉시 강제 종료 (사용자가 hung 상태에서 빠져나갈 수 있도록)
- 스레드: signal.signal 은 메인 스레드에서만 호출 가능

관련 모듈 (Related):
- src/risk/_state.py    — StopState 공유
- scripts/run_paper_trading.py — Task 32, install_sigint_handler 호출
"""
from __future__ import annotations
import signal
import sys
from typing import Optional

from ._state import StopState, StopReason, get_stop_state


# ============================================================
# 모듈 레벨 카운터 — 두 번째 SIGINT 즉시 종료용
# ============================================================
_sigint_count = 0


def install_sigint_handler(state: Optional[StopState] = None) -> None:
    """SIGINT(Ctrl-C) 핸들러 설치.

    설치 후 Ctrl-C 시:
      1회: state.request_stop(SIGINT) — graceful shutdown
      2회: 즉시 sys.exit(130) — 강제 종료 (130 = 128 + SIGINT)

    SECURITY:
      핸들러는 시그널 정보 외 다른 데이터에 접근하지 않는다.
      시그널 발생 시점의 stack frame 정보는 로깅하지 않음 (잠재적 시크릿 노출 방지).

    Args:
        state: 사용할 StopState. None 이면 전역 싱글톤.

    Note:
        signal.signal 은 Python 의 메인 스레드에서만 호출 가능.
        보조 스레드에서 호출 시 ValueError 발생.
    """
    state = state or get_stop_state()

    def _handler(signum: int, frame) -> None:  # noqa: ARG001 frame 미사용
        global _sigint_count
        _sigint_count += 1
        if _sigint_count == 1:
            # 첫 번째 SIGINT: graceful 정지 요청
            state.request_stop(
                reason=StopReason.SIGINT,
                note=f"signal={signum} (first interrupt)",
            )
        else:
            # 두 번째 이후: 즉시 강제 종료
            # async-signal-safe 한 sys.exit 사용
            sys.stderr.write(
                "\n[shutdown] second SIGINT received — forcing exit.\n"
            )
            sys.stderr.flush()
            sys.exit(130)

    signal.signal(signal.SIGINT, _handler)


def uninstall_sigint_handler() -> None:
    """기본 SIGINT 핸들러로 복구. 테스트 후 정리용.

    프로덕션 운영 중에는 호출하지 않는다 — 비상 정지 입력을 잃게 됨.
    """
    global _sigint_count
    _sigint_count = 0
    signal.signal(signal.SIGINT, signal.SIG_DFL)


def _get_sigint_count_for_testing() -> int:
    """테스트 전용: 현재까지 받은 SIGINT 횟수 조회."""
    return _sigint_count


def _reset_count_for_testing() -> None:
    """테스트 전용: 카운터 초기화."""
    global _sigint_count
    _sigint_count = 0
