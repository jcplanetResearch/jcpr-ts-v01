"""src/risk — 리스크 게이트, 비상 정지, 한도 관리 패키지.

본 단계 (Task 29-31) 산출물:
  - StopState, StopReason, StopEvent: 공유 정지 상태
  - install_sigint_handler: Ctrl-C 핸들러 (Task 29)
  - EscKeyListener:         ESC 키 리스너 (Task 30)
  - KillSwitchMonitor:      파일 기반 kill switch (Task 31)

추후 단계:
  - Task 19: risk_gate.py (사전 리스크 게이트)
  - Task 20: reports.py (리스크 거부 리포팅)
  - Task 46: capital_allocation.py
  - Task 47: portfolio_risk.py
"""
from ._state import (
    StopState,
    StopReason,
    StopEvent,
    get_stop_state,
)
from .shutdown import (
    install_sigint_handler,
    uninstall_sigint_handler,
)
from .keyboard_stop import EscKeyListener
from .kill_switch import KillSwitchMonitor

__all__ = [
    # state
    "StopState",
    "StopReason",
    "StopEvent",
    "get_stop_state",
    # shutdown (Task 29)
    "install_sigint_handler",
    "uninstall_sigint_handler",
    # keyboard (Task 30)
    "EscKeyListener",
    # kill switch (Task 31)
    "KillSwitchMonitor",
]
