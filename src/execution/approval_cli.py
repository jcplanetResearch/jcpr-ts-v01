"""
CLI 승인 제공자 (CLI Approval Provider)
========================================

JCPR Trading System - jcpr-ts-v01
Task 40 v0.1

터미널에서 인간 승인을 받는 ApprovalProvider 구현.
(Interactive terminal approval — implements ApprovalProvider.)

원칙 (Principles):
- Default deny on timeout (fail-closed)
- 명시적 입력 강제 (단순 Enter 거부)
- 모든 결정 audit log
- 테스트 주입 가능 (input_provider, output_writer)

응답 매핑 (Response Mapping):
- 승인 (loose):  'approve', 'a', 'yes', 'y', 'ok'
- 승인 (strict): 'approve', 'yes' 만
- 거부:          'reject', 'r', 'no', 'n', 'deny'
- 종료:          'quit', 'q', 'exit'
- 그 외 / 빈 입력: 거부
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional, TextIO

from .approval import ApprovalDecision, ApprovalProvider, ApprovalRequest
from .approval_audit import ApprovalAuditLog

logger = logging.getLogger(__name__)


# 응답 매핑
_APPROVE_LOOSE = {"approve", "a", "yes", "y", "ok"}
_APPROVE_STRICT = {"approve", "yes"}
_REJECT = {"reject", "r", "no", "n", "deny", "d"}
_QUIT = {"quit", "q", "exit"}


# ANSI 색상 (selective use)
_GREEN = "\033[92m"
_RED = "\033[91m"
_YELLOW = "\033[93m"
_CYAN = "\033[96m"
_BOLD = "\033[1m"
_RESET = "\033[0m"


class CLIApprovalProvider(ApprovalProvider):
    """
    터미널 인터랙티브 승인 Provider.

    Args:
        timeout_sec: 응답 대기 시간 (None이면 무한 대기)
        require_explicit_yes: True면 'approve'/'yes'만 허용 (단축 거부)
        deny_on_timeout: True면 타임아웃 시 거부 (기본)
        audit_log: ApprovalAuditLog (None이면 미기록)
        input_provider: 테스트 주입용 (기본 sys.stdin)
        output_writer: 테스트 주입용 (기본 sys.stdout)
        approver_id: audit log에 기록될 사용자 식별자
        use_color: ANSI 색상 사용
    """

    name = "cli_human"

    def __init__(
        self,
        *,
        timeout_sec: Optional[float] = 120.0,
        require_explicit_yes: bool = True,
        deny_on_timeout: bool = True,
        audit_log: Optional[ApprovalAuditLog] = None,
        input_provider: Optional[Callable[[str], str]] = None,
        output_writer: Optional[TextIO] = None,
        approver_id: str = "cli_human",
        use_color: bool = True,
    ):
        if timeout_sec is not None and timeout_sec <= 0:
            raise ValueError(f"timeout_sec 양수 또는 None 필요: {timeout_sec}")
        self._timeout_sec = timeout_sec
        self._require_explicit = require_explicit_yes
        self._deny_on_timeout = deny_on_timeout
        self._audit_log = audit_log
        self._input_provider = input_provider
        self._output = output_writer or sys.stdout
        self._approver_id = approver_id
        self._use_color = use_color

    # ------------------------------------------------------------------
    # 메인
    # ------------------------------------------------------------------

    def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        start = time.monotonic()

        self._render_request(request)

        # 입력 수집
        try:
            response = self._collect_input()
        except _InputTimeout:
            decision = self._build_decision(
                approved=False,
                reason=f"timeout ({self._timeout_sec}s) — fail-closed",
            )
            self._render_outcome(decision)
            self._audit(request, decision, response_time_sec=self._timeout_sec)
            return decision
        except (KeyboardInterrupt, EOFError):
            decision = self._build_decision(
                approved=False,
                reason="user interrupted (Ctrl-C / EOF)",
            )
            self._render_outcome(decision)
            self._audit(request, decision, response_time_sec=time.monotonic() - start)
            return decision

        elapsed = time.monotonic() - start
        approved, reason = self._classify_response(response)
        decision = self._build_decision(approved=approved, reason=reason)
        self._render_outcome(decision)
        self._audit(request, decision, response_time_sec=elapsed)
        return decision

    # ------------------------------------------------------------------
    # 렌더링
    # ------------------------------------------------------------------

    def _c(self, color: str, text: str) -> str:
        return f"{color}{text}{_RESET}" if self._use_color else text

    def _render_request(self, request: ApprovalRequest) -> None:
        out = self._output
        b = lambda s: self._c(_BOLD, s)
        cyan = lambda s: self._c(_CYAN, s)
        yellow = lambda s: self._c(_YELLOW, s)

        side_label = "매수 (BUY)" if request.side == "buy" else "매도 (SELL)"
        live_warn = (
            yellow("⚠️  실거래 환경 (LIVE)") if request.is_live_env
            else self._c(_GREEN, "모의투자 (PAPER)")
        )
        send_warn = (
            yellow("⚠️  실 송신 활성화 (LIVE ORDERS)") if not request.is_dry_run
            else self._c(_GREEN, "DRY-RUN (실 송신 차단)")
        )

        lines = [
            "",
            b("=" * 60),
            b("  주문 승인 요청 (Order Approval Request)"),
            b("=" * 60),
            f"  Execution ID:    {request.execution_id}",
            f"  Signal ID:       {request.signal_id or '<none>'}",
            f"  종목 (Symbol):   {cyan(request.symbol)}",
            f"  방향 (Side):     {side_label}",
            f"  수량 (Quantity): {request.quantity:,} 주",
            f"  가격 (Price):    {int(request.price):,} KRW",
            f"  예상 비용:       {int(request.estimated_cost_krw):,} KRW",
            "",
            f"  환경:           {live_warn}",
            f"  주문 송신:      {send_warn}",
            f"  요청 시각:      {request.requested_at_utc.isoformat()}",
            b("=" * 60),
        ]

        if self._require_explicit:
            prompt_help = "승인: 'approve' 또는 'yes' 입력 / 거부: 'reject' 또는 'no' / 종료: 'quit'"
        else:
            prompt_help = "승인: a/y/yes / 거부: n/no/reject / 종료: q/quit"

        lines.append(prompt_help)
        if self._timeout_sec is not None:
            lines.append(f"⏱  {int(self._timeout_sec)}초 내 응답 없으면 자동 거부")

        for line in lines:
            print(line, file=out)
        out.flush()

    def _render_outcome(self, decision: ApprovalDecision) -> None:
        out = self._output
        if decision.approved:
            print(self._c(_GREEN, f"✅ 승인됨 (APPROVED): {decision.reason}"), file=out)
        else:
            print(self._c(_RED, f"❌ 거부됨 (REJECTED): {decision.reason}"), file=out)
        print("", file=out)
        out.flush()

    # ------------------------------------------------------------------
    # 입력 수집
    # ------------------------------------------------------------------

    def _collect_input(self) -> str:
        prompt = "[승인 / 거부 / 종료]: "
        if self._input_provider is not None:
            # 테스트용 — 즉시 반환 (타임아웃 무시)
            return self._input_provider(prompt)

        if self._timeout_sec is None:
            # 무한 대기
            return input(prompt)

        # 타임아웃 입력 (스레드 + Event)
        return _input_with_timeout(prompt, self._timeout_sec, output=self._output)

    def _classify_response(self, raw: str) -> tuple[bool, str]:
        """raw 입력 → (approved, reason)."""
        s = (raw or "").strip().lower()
        if not s:
            return False, "empty input — fail-closed"

        if s in _QUIT:
            return False, f"user requested quit ({s!r})"
        if s in _REJECT:
            return False, f"user rejected ({s!r})"

        if self._require_explicit:
            if s in _APPROVE_STRICT:
                return True, f"explicit human approval ({s!r})"
            if s in _APPROVE_LOOSE - _APPROVE_STRICT:
                return False, (
                    f"shortcut not allowed (require_explicit_yes=True): "
                    f"got {s!r}, expected 'approve' or 'yes'"
                )
            return False, f"unknown response: {s!r}"
        else:
            if s in _APPROVE_LOOSE:
                return True, f"human approval ({s!r})"
            return False, f"unknown response: {s!r}"

    # ------------------------------------------------------------------
    # Decision + Audit
    # ------------------------------------------------------------------

    def _build_decision(self, *, approved: bool, reason: str) -> ApprovalDecision:
        return ApprovalDecision(
            approved=approved,
            reason=reason,
            decided_at_utc=datetime.now(timezone.utc),
            approver=self._approver_id,
        )

    def _audit(
        self,
        request: ApprovalRequest,
        decision: ApprovalDecision,
        *,
        response_time_sec: float,
    ) -> None:
        if self._audit_log is None:
            return
        try:
            self._audit_log.write(
                request, decision,
                response_time_sec=response_time_sec,
                provider_chain=[self.name],
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Approval audit 기록 실패: %s", e)


# ─────────────────────────────────────────────────
# 타임아웃 입력 헬퍼
# ─────────────────────────────────────────────────

class _InputTimeout(Exception):
    """입력 타임아웃."""


def _input_with_timeout(
    prompt: str, timeout_sec: float, *, output: TextIO,
) -> str:
    """
    별도 스레드로 input() 실행 → join(timeout).
    타임아웃이면 _InputTimeout 발생.
    
    참고: 표준 입력이 닫혀있는 환경(non-tty)에서는 input()이 즉시 EOF 반환 가능.
    """
    print(prompt, end="", file=output, flush=True)

    result_box: dict = {"value": None, "error": None}

    def reader():
        try:
            result_box["value"] = input()
        except (KeyboardInterrupt, EOFError) as e:
            result_box["error"] = e

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    t.join(timeout=timeout_sec)

    if t.is_alive():
        # 타임아웃 — 스레드는 데몬이므로 프로세스 종료 시 자동 정리
        raise _InputTimeout()

    if result_box["error"] is not None:
        raise result_box["error"]
    return result_box["value"] or ""
