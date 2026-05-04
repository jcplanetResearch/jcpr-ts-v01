"""RiskGateContext — 검사 함수에 전달되는 입력 묶음.

본 객체는 게이트 호출 시점의 시스템 상태 스냅샷이다.
호출자(execution_gateway, Task 21)가 채워서 게이트에 전달.

설계:
- dataclass 로 명시적 필드 — 타입 안전성 확보
- 모든 시간은 UTC tz-aware
- 모든 금액은 Decimal
- mutable (사용자가 채우는 컨테이너 역할)
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional, Sequence

from src.brokers import OrderIntent, Position, Quote
from src.data import KrxCalendar
from src.risk._state import StopState
from src.risk.kill_switch import KillSwitchMonitor
from src.risk._history import OrderHistory


@dataclass
class RiskGateContext:
    """게이트 검사 시점의 모든 입력.

    Required:
        intent, now_utc, stop_state, kill_switch, calendar, history,
        capacity_config, risk_limits_config

    Optional:
        cash_balance, total_equity (잔고 검사용)
        positions (포지션 한도 검사용)
        open_orders_count (주문 한도 검사용)
        quote (가격·슬리피지 검사용)
        session_realized_pnl, session_unrealized_pnl, session_high_equity (loss 검사용)
    """

    # === 검사 대상 ===
    intent: OrderIntent
    now_utc: datetime  # tz-aware UTC

    # === 시스템 안전 상태 ===
    stop_state: StopState
    kill_switch: KillSwitchMonitor

    # === 시장 상태 ===
    calendar: KrxCalendar

    # === 주문 이력 ===
    history: OrderHistory

    # === 설정 (configs/*.yaml 로드 결과) ===
    capacity_config: dict[str, Any]
    risk_limits_config: dict[str, Any]

    # === 자본·잔고 (선택; 없으면 해당 검사가 fail-closed 거부) ===
    cash_balance: Decimal = Decimal(0)
    total_equity: Decimal = Decimal(0)

    # === 포지션 ===
    positions: Sequence[Position] = field(default_factory=list)
    open_orders_count: int = 0

    # === 시세 (LIMIT 주문이면 quote 가 필수) ===
    quote: Optional[Quote] = None

    # === 손익 (loss limits 검사용) ===
    session_realized_pnl: Decimal = Decimal(0)
    session_unrealized_pnl: Decimal = Decimal(0)
    session_high_equity: Decimal = Decimal(0)
