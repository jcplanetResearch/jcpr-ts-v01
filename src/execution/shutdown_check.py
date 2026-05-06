"""
Stop-First 통합 점검 (Stop-First Integrated Check)
====================================================

JCPR Trading System - jcpr-ts-v01
Task 21 v0.1

ExecutionGateway가 매 단계 시작 시 호출하는 통합 종료 신호 점검.
(Integrated shutdown check called at each stage.)

통합 신호 (Combined signals):
- Kill switch file (Task 31 — runtime/KILL_SWITCH_ON)
- Shutdown event (Task 29/30 — Ctrl-C, ESC → threading.Event)

원칙: 어느 하나라도 active이면 즉시 종료 (stop-first).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class ShutdownStatus:
    """종료 신호 상태."""
    active: bool
    reason: Optional[str] = None
    detail: dict = None  # type: ignore[assignment]


class ShutdownChecker:
    """
    Kill switch 파일 + Shutdown event 통합 점검.

    Args:
        kill_switch_path: Task 31 킬 스위치 파일 (없으면 점검 안 함)
        shutdown_event: Task 29/30 shutdown event (없으면 점검 안 함)
    """

    def __init__(
        self,
        kill_switch_path: Optional[str | Path] = "runtime/KILL_SWITCH_ON",
        shutdown_event: Optional[threading.Event] = None,
    ):
        self._kill_path = Path(kill_switch_path) if kill_switch_path else None
        self._shutdown_event = shutdown_event

    def check(self) -> ShutdownStatus:
        """
        종료 신호 점검. active이면 reason 포함.
        """
        if self._kill_path is not None and self._kill_path.exists():
            return ShutdownStatus(
                active=True,
                reason="kill_switch_active",
                detail={"path": str(self._kill_path)},
            )

        if self._shutdown_event is not None and self._shutdown_event.is_set():
            return ShutdownStatus(
                active=True,
                reason="shutdown_event_set",
                detail={"event": "set"},
            )

        return ShutdownStatus(active=False)

    @property
    def kill_switch_path(self) -> Optional[Path]:
        return self._kill_path
