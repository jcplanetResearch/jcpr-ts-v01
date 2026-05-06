"""Kill-switch 게이트 (Kill-switch Gate) — Task 31 연동."""

from __future__ import annotations

from pathlib import Path

from .base import GateResult, RiskContext, RiskGate


class KillSwitchGate(RiskGate):
    """
    runtime/KILL_SWITCH_ON 파일이 존재하면 모든 주문 거부.
    (Reject all orders if runtime/KILL_SWITCH_ON file exists.)

    stop-first 원칙 — 가장 먼저 평가되어야 함.
    """

    name = "kill_switch"

    def __init__(self, kill_switch_path: str | Path = "runtime/KILL_SWITCH_ON"):
        self._path = Path(kill_switch_path)

    def evaluate(self, ctx: RiskContext) -> GateResult:
        if self._path.exists():
            return GateResult(
                gate_name=self.name,
                outcome="reject",
                reason="킬 스위치 활성화 (kill switch active)",
                detail={"path": str(self._path)},
            )
        return GateResult(gate_name=self.name, outcome="pass", reason=None)
