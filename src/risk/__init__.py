"""src/risk — 리스크 게이트, 비상 정지, 한도 관리 패키지.

본 단계 (Task 19, 29-31) 산출물:
  - StopState, StopReason, StopEvent: 공유 정지 상태
  - install_sigint_handler: Ctrl-C 핸들러 (Task 29)
  - EscKeyListener:         ESC 키 리스너 (Task 30)
  - KillSwitchMonitor:      파일 기반 kill switch (Task 31)
  - RiskGate:               사전 리스크 게이트 (Task 19)
  - RiskGateContext:        게이트 입력 컨텍스트
  - GateDecision:           게이트 결정
  - CheckResult:            단일 검사 결과
  - RejectionReason:        17개 거부 사유 분류
  - OrderHistory:           최근 주문 이력 (빈도/중복 검사용)

추후 단계:
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

# Task 19
from ._decision import (
    CheckResult,
    GateDecision,
    RejectionReason,
)
from ._context import RiskGateContext
from ._history import OrderHistory, OrderRecord
from .risk_gate import RiskGate, DEFAULT_CHECK_ORDER

__all__ = [
    # state
    "StopState", "StopReason", "StopEvent", "get_stop_state",
    # shutdown (Task 29)
    "install_sigint_handler", "uninstall_sigint_handler",
    # keyboard (Task 30)
    "EscKeyListener",
    # kill switch (Task 31)
    "KillSwitchMonitor",
    # risk gate (Task 19)
    "RiskGate", "DEFAULT_CHECK_ORDER",
    "RiskGateContext",
    "GateDecision", "CheckResult", "RejectionReason",
    "OrderHistory", "OrderRecord",
]
