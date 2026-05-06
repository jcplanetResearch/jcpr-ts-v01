"""
시그널 러너 v0.3 (Signal Runner v0.3)
======================================

JCPR Trading System - jcpr-ts-v01
Task 16 v0.3

시그널 생성과 실행 게이트웨이를 통합한 자동 사이클 실행기.
(Automated cycle runner integrating signal generation + execution gateway.)

이전 버전 대비 변경 (Changes from v0.2):
- ExecutionGateway (Task 21) 자동 연결 — 시그널 → 사이징 → 리스크 → 주문
- Sequential MVP, ≥5초 간격 강제 (요구사항)
- Stop-first 통합 (Ctrl-C/ESC/Kill switch 즉시 응답)
- 사이클 통계 + audit log
- run_forever 무한 루프 모드

원칙 (Principles):
- ≥5초 종목간 최소 간격 강제
- Sleep 중에도 shutdown 100ms 이내 응답
- 시장 미개장 시 자동 skip 옵션
- 모든 audit log 비밀 미포함
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Literal, Optional
from uuid import uuid4

from ..data.ohlcv_schema import Timeframe
from ..data.symbol_master import SymbolMaster
from ..execution.execution_record import ExecutionOutcome, ExecutionResult
from ..execution.gateway import ExecutionGateway
from ..execution.shutdown_check import ShutdownChecker
from .runner_audit import CycleAuditLog, build_cycle_record
from .schema_v2 import MomentumSignalV04, SignalSide
from .strategies.momentum_v04 import MomentumStrategyV04

logger = logging.getLogger(__name__)


WatchlistMode = Literal["auto", "explicit"]


# ─────────────────────────────────────────────────
# 설정 + 결과 모델
# ─────────────────────────────────────────────────

@dataclass(frozen=True)
class RunnerConfig:
    """SignalRunner 설정."""
    timeframe: Timeframe = Timeframe.D1
    watchlist_mode: WatchlistMode = "explicit"   # 기본 explicit (안전)
    explicit_watchlist: tuple[str, ...] = ()     # explicit 모드에서 사용
    min_symbol_interval_sec: int = 5             # ≥5초 (요구사항)
    cycle_interval_sec: int = 60                 # run_forever 사이클 간격
    skip_when_market_closed: bool = True
    daily_realized_pnl_krw_provider: Optional[callable] = None  # 외부에서 제공
    shutdown_poll_interval_sec: float = 0.1      # sleep 중 shutdown 점검 주기


@dataclass(frozen=True)
class CycleStats:
    """사이클 통계."""
    total_symbols: int
    submitted: int
    skipped: int
    rejected: int
    errored: int
    elapsed_sec: float

    def as_dict(self) -> dict:
        return {
            "total_symbols": self.total_symbols,
            "submitted": self.submitted,
            "skipped": self.skipped,
            "rejected": self.rejected,
            "errored": self.errored,
            "elapsed_sec": round(self.elapsed_sec, 3),
        }


@dataclass(frozen=True)
class CycleResult:
    """사이클 결과."""
    cycle_id: str
    started_at_utc: datetime
    completed_at_utc: datetime
    stats: CycleStats
    executions: list[ExecutionResult] = field(default_factory=list)
    aborted: bool = False
    abort_reason: Optional[str] = None


class CycleAborted(RuntimeError):
    """사이클 중단 (stop-first)."""


# ─────────────────────────────────────────────────
# SignalRunner
# ─────────────────────────────────────────────────

class SignalRunner:
    """
    시그널 자동 생성 + 실행 러너.

    의존성 (Dependencies):
        strategy: MomentumStrategyV04 (Task 14 v0.4)
        gateway:  ExecutionGateway (Task 21)
        symbol_master: SymbolMaster (Task 10)
        shutdown: ShutdownChecker (stop-first)
        cycle_audit: CycleAuditLog (사이클 기록)
    """

    def __init__(
        self,
        *,
        strategy: MomentumStrategyV04,
        gateway: ExecutionGateway,
        symbol_master: SymbolMaster,
        shutdown: Optional[ShutdownChecker] = None,
        cycle_audit: Optional[CycleAuditLog] = None,
        config: Optional[RunnerConfig] = None,
        market_is_open_provider: Optional[callable] = None,
    ):
        self._strategy = strategy
        self._gateway = gateway
        self._sm = symbol_master
        self._shutdown = shutdown or ShutdownChecker()
        self._audit = cycle_audit
        self._cfg = config or RunnerConfig()
        # market 상태 판정 콜백 — Task 11 calendar에서 주입
        self._market_open_fn = market_is_open_provider or (lambda now: True)

        # 종목간 최소 간격 검증
        if self._cfg.min_symbol_interval_sec < 5:
            raise ValueError("min_symbol_interval_sec >= 5 (요구사항)")

        # explicit 모드에서 watchlist 비어있으면 거부 (fail-closed)
        if self._cfg.watchlist_mode == "explicit" and not self._cfg.explicit_watchlist:
            raise ValueError(
                "watchlist_mode='explicit'인데 explicit_watchlist가 비어있음 — "
                "RunnerConfig.explicit_watchlist에 종목 코드 지정 필요"
            )

        # 직전 종목/사이클 추적 (rate limit용)
        self._last_order_at_utc: Optional[datetime] = None
        self._last_order_per_symbol_utc: dict[str, datetime] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_cycle(self) -> CycleResult:
        """
        단일 사이클 실행.
        watchlist 내 모든 종목을 ≥5초 간격으로 순차 처리.
        """
        cycle_id = f"cycle-{uuid4().hex[:12]}"
        started_at = datetime.now(timezone.utc)
        executions: list[ExecutionResult] = []
        per_symbol_summary: list[dict] = []
        aborted = False
        abort_reason: Optional[str] = None

        # 사이클 시작 시 stop-first
        sd = self._shutdown.check()
        if sd.active:
            return self._build_aborted_cycle(
                cycle_id, started_at, executions, per_symbol_summary,
                reason=f"shutdown at cycle start: {sd.reason}",
            )

        # 시장 개장 점검
        market_is_open = self._market_open_fn(started_at)
        if not market_is_open and self._cfg.skip_when_market_closed:
            logger.info("[runner] 시장 미개장 — 사이클 skip")
            return self._build_aborted_cycle(
                cycle_id, started_at, executions, per_symbol_summary,
                reason="market_closed",
            )

        # Watchlist 결정
        try:
            watchlist = self._resolve_watchlist()
        except ValueError as e:
            return self._build_aborted_cycle(
                cycle_id, started_at, executions, per_symbol_summary,
                reason=f"watchlist resolve failed: {e}",
            )

        logger.info(
            "[runner] 사이클 시작: cycle_id=%s, watchlist=%d종목, market_open=%s",
            cycle_id, len(watchlist), market_is_open,
        )

        # 종목별 순차 처리
        last_processed_at: Optional[datetime] = None
        for idx, symbol in enumerate(watchlist):
            # Stop-first 점검
            sd = self._shutdown.check()
            if sd.active:
                aborted = True
                abort_reason = f"shutdown during cycle: {sd.reason}"
                logger.info("[runner] 사이클 중단: %s", abort_reason)
                break

            # 종목간 최소 간격 보장 (첫 종목 제외)
            if idx > 0 and last_processed_at is not None:
                try:
                    self._wait_min_interval(last_processed_at)
                except CycleAborted as ca:
                    aborted = True
                    abort_reason = str(ca)
                    logger.info("[runner] 사이클 중단 (interval wait): %s", abort_reason)
                    break

            # 종목 처리
            try:
                exec_result = self._process_symbol(symbol, market_is_open)
            except Exception as e:  # noqa: BLE001
                logger.exception("[runner] 종목 처리 예외: %s", symbol)
                exec_result = None
                per_symbol_summary.append({
                    "symbol": symbol,
                    "outcome": "error",
                    "reason": f"{type(e).__name__}: {e}",
                })
            else:
                if exec_result is None:
                    # 시그널이 strategy 단계에서 None 또는 처리 불필요
                    per_symbol_summary.append({
                        "symbol": symbol,
                        "outcome": "no_signal",
                        "reason": None,
                    })
                else:
                    executions.append(exec_result)
                    per_symbol_summary.append({
                        "symbol": symbol,
                        "outcome": exec_result.outcome.value,
                        "stage": exec_result.final_stage.value,
                        "reason": exec_result.reject_reason,
                        "execution_id": exec_result.execution_id,
                    })
                    # rate limit용 시각 갱신 (실제 송신된 경우만)
                    if exec_result.outcome == ExecutionOutcome.SUBMITTED:
                        self._last_order_at_utc = exec_result.completed_at_utc
                        self._last_order_per_symbol_utc[symbol] = exec_result.completed_at_utc

            last_processed_at = datetime.now(timezone.utc)

        # 사이클 완료
        completed_at = datetime.now(timezone.utc)
        stats = self._compute_stats(executions, len(watchlist), started_at, completed_at)

        result = CycleResult(
            cycle_id=cycle_id,
            started_at_utc=started_at,
            completed_at_utc=completed_at,
            stats=stats,
            executions=executions,
            aborted=aborted,
            abort_reason=abort_reason,
        )

        # Audit log
        if self._audit is not None:
            self._audit.write(build_cycle_record(
                cycle_id=cycle_id,
                started_at_utc=started_at,
                completed_at_utc=completed_at,
                watchlist=list(watchlist),
                stats=stats.as_dict(),
                aborted=aborted,
                abort_reason=abort_reason,
                per_symbol_summary=per_symbol_summary,
                metadata={"timeframe": self._cfg.timeframe.value},
            ))

        logger.info(
            "[runner] 사이클 완료: cycle_id=%s stats=%s aborted=%s",
            cycle_id, stats.as_dict(), aborted,
        )
        return result

    def run_forever(self, *, max_cycles: Optional[int] = None) -> int:
        """
        장 마감 / shutdown / max_cycles 도달까지 사이클 반복.

        Args:
            max_cycles: None이면 무한, 정수면 최대 N회 (테스트용)

        Returns:
            실행된 사이클 수
        """
        cycle_count = 0
        while True:
            if max_cycles is not None and cycle_count >= max_cycles:
                logger.info("[runner] max_cycles 도달 (%d)", max_cycles)
                return cycle_count

            sd = self._shutdown.check()
            if sd.active:
                logger.info("[runner] run_forever 종료: shutdown=%s", sd.reason)
                return cycle_count

            self.run_cycle()
            cycle_count += 1

            # 다음 사이클까지 대기
            try:
                self._wait_seconds(self._cfg.cycle_interval_sec)
            except CycleAborted as ca:
                logger.info("[runner] run_forever 종료 (cycle wait): %s", ca)
                return cycle_count

    # ------------------------------------------------------------------
    # 내부 헬퍼
    # ------------------------------------------------------------------

    def _resolve_watchlist(self) -> list[str]:
        """설정에 따라 watchlist 결정."""
        if self._cfg.watchlist_mode == "explicit":
            wl = list(self._cfg.explicit_watchlist)
        else:  # auto
            wl = self._sm.tradable_codes()

        # explicit 모드에서도 Symbol Master 검증 (fail-closed)
        verified: list[str] = []
        for code in wl:
            if self._sm.is_tradable(code):
                verified.append(code)
            else:
                logger.warning("[runner] watchlist 종목 거래 불가 — 제외: %s", code)
        if not verified:
            raise ValueError("watchlist에 거래 가능 종목 0개")
        return verified

    def _process_symbol(
        self, symbol: str, market_is_open: bool,
    ) -> Optional[ExecutionResult]:
        """단일 종목 처리: 시그널 생성 → 게이트웨이 실행."""
        as_of = datetime.now(timezone.utc)

        # 시그널 생성
        signal: MomentumSignalV04 = self._strategy.generate(
            symbol, self._cfg.timeframe, as_of,
        )

        # FLAT은 게이트웨이로 보내도 SKIPPED 반환되지만,
        # 효율을 위해 여기서 조기 종료 — None 반환 (no_signal 기록)
        if signal.side == SignalSide.FLAT:
            return None

        # 외부 P&L 제공자 (있으면)
        try:
            daily_pnl = (
                self._cfg.daily_realized_pnl_krw_provider()
                if self._cfg.daily_realized_pnl_krw_provider else None
            )
        except Exception:  # noqa: BLE001
            daily_pnl = None

        # 게이트웨이 호출
        return self._gateway.execute(
            signal,
            last_order_at_utc=self._last_order_at_utc,
            last_order_for_symbol_utc=self._last_order_per_symbol_utc.get(symbol),
            daily_realized_pnl_krw=daily_pnl if daily_pnl is not None else __import__("decimal").Decimal("0"),
            market_is_open=market_is_open,
        )

    def _wait_min_interval(self, last_processed_at: datetime) -> None:
        """
        직전 종목 처리 후 ≥min_symbol_interval_sec 보장.
        Sleep 중에도 shutdown 점검 (≤100ms 응답).
        """
        elapsed = (datetime.now(timezone.utc) - last_processed_at).total_seconds()
        remaining = self._cfg.min_symbol_interval_sec - elapsed
        if remaining <= 0:
            return
        self._wait_seconds(remaining)

    def _wait_seconds(self, seconds: float) -> None:
        """
        N초 대기 + shutdown 점검.
        (Wait N seconds with periodic shutdown check.)
        """
        if seconds <= 0:
            return
        deadline = time.monotonic() + seconds
        poll = self._cfg.shutdown_poll_interval_sec
        while True:
            now_mono = time.monotonic()
            if now_mono >= deadline:
                return
            sd = self._shutdown.check()
            if sd.active:
                raise CycleAborted(f"shutdown during wait: {sd.reason}")
            time.sleep(min(poll, deadline - now_mono))

    @staticmethod
    def _compute_stats(
        executions: list[ExecutionResult],
        total_symbols: int,
        started: datetime,
        completed: datetime,
    ) -> CycleStats:
        submitted = sum(1 for e in executions if e.outcome == ExecutionOutcome.SUBMITTED)
        skipped = sum(1 for e in executions if e.outcome == ExecutionOutcome.SKIPPED)
        rejected = sum(1 for e in executions if e.outcome == ExecutionOutcome.REJECTED)
        errored = sum(1 for e in executions if e.outcome == ExecutionOutcome.ERROR)
        return CycleStats(
            total_symbols=total_symbols,
            submitted=submitted,
            skipped=skipped,
            rejected=rejected,
            errored=errored,
            elapsed_sec=(completed - started).total_seconds(),
        )

    def _build_aborted_cycle(
        self,
        cycle_id: str,
        started_at: datetime,
        executions: list[ExecutionResult],
        per_symbol_summary: list[dict],
        *,
        reason: str,
    ) -> CycleResult:
        completed_at = datetime.now(timezone.utc)
        stats = self._compute_stats(executions, 0, started_at, completed_at)
        result = CycleResult(
            cycle_id=cycle_id,
            started_at_utc=started_at,
            completed_at_utc=completed_at,
            stats=stats,
            executions=executions,
            aborted=True,
            abort_reason=reason,
        )
        if self._audit is not None:
            self._audit.write(build_cycle_record(
                cycle_id=cycle_id,
                started_at_utc=started_at,
                completed_at_utc=completed_at,
                watchlist=[],
                stats=stats.as_dict(),
                aborted=True,
                abort_reason=reason,
                per_symbol_summary=per_symbol_summary,
            ))
        return result
