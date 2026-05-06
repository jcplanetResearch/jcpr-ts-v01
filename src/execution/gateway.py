"""
실행 게이트웨이 (Execution Gateway)
=====================================

JCPR Trading System - jcpr-ts-v01
Task 21 v0.1

전체 파이프라인 통합:
시그널 → 검증 → 멱등성 → 계좌조회 → 사이징 → 리스크게이트 → 승인 → 주문송신

원칙 (Principles):
- Stop-first: 매 단계 시작 시 종료 신호 점검
- Fail-closed: 어떤 단계든 실패/거부 → 즉시 종료
- Sequential: 한 번에 하나의 시그널만 처리 (≥5초 간격은 RateLimitGate가 처리)
- Audit: 모든 실행 → JSONL audit log
- Idempotent: signal_id 24시간 쿨다운

Returns: ExecutionResult — 호출자가 next_step 결정 가능
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from ..brokers.kis.adapter import KISAdapter
from ..brokers.kis.orders import OrderRequest, OrderSide, OrderType
from ..data.symbol_master import SymbolMaster
from ..risk.gates.base import RiskContext
from ..risk.risk_gate import RiskGateRunner
from ..signals.schema_v2 import MomentumSignalV04, SignalSide

from .approval import ApprovalProvider, ApprovalRequest, AutoApproveProvider
from .execution_record import (
    ExecutionAuditLog,
    ExecutionOutcome,
    ExecutionResult,
    ExecutionStage,
    build_record,
    compute_signal_id,
    new_execution_id,
)
from .shutdown_check import ShutdownChecker
from .sizing import OrderSizer, SizingInputs

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────
# Execution Gateway
# ─────────────────────────────────────────────────

class ExecutionGateway:
    """
    Task 14 v0.4 시그널 → KIS 주문 송신까지 전체 파이프라인.

    Components (모두 의존성 주입):
        adapter:        KISAdapter (Task 8)
        symbol_master:  SymbolMaster (Task 10)
        sizer:          OrderSizer (Task 18)
        risk_runner:    RiskGateRunner (Task 19)
        audit_log:      ExecutionAuditLog
        approval:       ApprovalProvider (default: AutoApprove)
        shutdown:       ShutdownChecker
    """

    def __init__(
        self,
        adapter: KISAdapter,
        symbol_master: SymbolMaster,
        sizer: OrderSizer,
        risk_runner: RiskGateRunner,
        audit_log: ExecutionAuditLog,
        *,
        approval: Optional[ApprovalProvider] = None,
        shutdown: Optional[ShutdownChecker] = None,
        min_signal_confidence: Decimal = Decimal("0.5"),
        last_quote_age_max_sec: int = 30,
    ):
        self._adapter = adapter
        self._sm = symbol_master
        self._sizer = sizer
        self._risk = risk_runner
        self._audit = audit_log
        self._approval = approval or AutoApproveProvider()
        self._shutdown = shutdown or ShutdownChecker()
        self._min_conf = min_signal_confidence
        self._max_quote_age = last_quote_age_max_sec

    # ------------------------------------------------------------------
    # 메인 진입점
    # ------------------------------------------------------------------

    def execute(
        self,
        signal: MomentumSignalV04,
        *,
        last_order_at_utc: Optional[datetime] = None,
        last_order_for_symbol_utc: Optional[datetime] = None,
        daily_realized_pnl_krw: Decimal = Decimal("0"),
        market_is_open: bool = True,
    ) -> ExecutionResult:
        """
        시그널 1건을 받아 전체 파이프라인 실행.
        (Execute full pipeline for one signal.)

        Args:
            signal: 시그널 (Task 14 v0.4 출력)
            last_order_at_utc: 직전 주문 시각 (rate limit용)
            last_order_for_symbol_utc: 동일 종목 직전 주문 시각
            daily_realized_pnl_krw: 오늘 누적 실현 P&L
            market_is_open: 시장 개장 여부 (Task 11 calendar 결과)

        Returns:
            ExecutionResult — outcome으로 다음 행동 결정
        """
        execution_id = new_execution_id()
        signal_id = compute_signal_id(
            signal.symbol, signal.strategy_id,
            signal.timestamp_utc, signal.side.value,
        )
        started_at = datetime.now(timezone.utc)
        stage_results: dict[str, Any] = {}
        metadata: dict[str, Any] = {
            "signal_score": str(signal.composite_score),
            "signal_confidence": str(signal.confidence),
        }

        logger.info(
            "[gateway] 시작: execution_id=%s signal_id=%s symbol=%s side=%s",
            execution_id, signal_id, signal.symbol, signal.side.value,
        )

        # ───────────────── [1] STOP-FIRST 점검 ─────────────────
        sd = self._shutdown.check()
        if sd.active:
            return self._finalize(
                execution_id, signal_id, signal,
                outcome=ExecutionOutcome.REJECTED,
                final_stage=ExecutionStage.STOP_CHECK,
                reject_reason=f"shutdown active: {sd.reason}",
                started_at=started_at,
                stage_results=stage_results,
                metadata={**metadata, "shutdown": sd.reason},
            )
        stage_results["stop_check"] = "pass"

        # ───────────────── [2] SIGNAL VALIDATION ─────────────────
        if signal.side == SignalSide.FLAT:
            return self._finalize(
                execution_id, signal_id, signal,
                outcome=ExecutionOutcome.SKIPPED,
                final_stage=ExecutionStage.SIGNAL_VALIDATION,
                reject_reason="signal side is FLAT",
                started_at=started_at,
                stage_results=stage_results,
                metadata=metadata,
            )

        if signal.confidence < self._min_conf:
            return self._finalize(
                execution_id, signal_id, signal,
                outcome=ExecutionOutcome.SKIPPED,
                final_stage=ExecutionStage.SIGNAL_VALIDATION,
                reject_reason=f"confidence {signal.confidence} < min {self._min_conf}",
                started_at=started_at,
                stage_results=stage_results,
                metadata=metadata,
            )

        if not self._sm.is_tradable(signal.symbol):
            return self._finalize(
                execution_id, signal_id, signal,
                outcome=ExecutionOutcome.REJECTED,
                final_stage=ExecutionStage.SIGNAL_VALIDATION,
                reject_reason=f"symbol not tradable: {signal.symbol}",
                started_at=started_at,
                stage_results=stage_results,
                metadata=metadata,
            )
        stage_results["signal_validation"] = "pass"

        # ───────────────── [3] IDEMPOTENCY ─────────────────
        if self._audit.is_duplicate(signal_id):
            return self._finalize(
                execution_id, signal_id, signal,
                outcome=ExecutionOutcome.SKIPPED,
                final_stage=ExecutionStage.IDEMPOTENCY_CHECK,
                reject_reason="duplicate signal_id within idempotency window (24h)",
                started_at=started_at,
                stage_results=stage_results,
                metadata=metadata,
            )
        stage_results["idempotency_check"] = "pass"

        # Stop-first 재점검
        sd = self._shutdown.check()
        if sd.active:
            return self._finalize(
                execution_id, signal_id, signal,
                outcome=ExecutionOutcome.REJECTED,
                final_stage=ExecutionStage.STOP_CHECK,
                reject_reason=f"shutdown active: {sd.reason}",
                started_at=started_at,
                stage_results=stage_results, metadata=metadata,
            )

        # ───────────────── [4] ACCOUNT SNAPSHOT ─────────────────
        try:
            account_snap = self._adapter.account.fetch_account_snapshot()
        except Exception as e:  # noqa: BLE001
            logger.exception("계좌 조회 실패")
            return self._finalize(
                execution_id, signal_id, signal,
                outcome=ExecutionOutcome.ERROR,
                final_stage=ExecutionStage.ACCOUNT_SNAPSHOT,
                reject_reason=f"account fetch failed: {type(e).__name__}: {e}",
                started_at=started_at,
                stage_results=stage_results, metadata=metadata,
            )
        stage_results["account_snapshot"] = "ok"
        metadata["equity_krw"] = str(account_snap.total_evaluation_krw)

        # ───────────────── [5] SIZING (Task 18) ─────────────────
        # Symbol master에서 instrument_type/lot_size 조회
        sym = self._sm.get(signal.symbol)
        # 시그널은 가격을 직접 주지 않으므로, 호가 또는 현재가 필요
        # v0.1: 호가에서 mid_quote 조회
        try:
            quote_snap = self._adapter.quote.snapshot(signal.symbol)
        except Exception as e:  # noqa: BLE001
            logger.exception("호가 조회 실패")
            return self._finalize(
                execution_id, signal_id, signal,
                outcome=ExecutionOutcome.ERROR,
                final_stage=ExecutionStage.SIZING,
                reject_reason=f"quote fetch failed: {type(e).__name__}: {e}",
                started_at=started_at,
                stage_results=stage_results, metadata=metadata,
            )

        ref_price = quote_snap.mid_quote()

        side_str = "buy" if signal.side == SignalSide.BUY else "sell"
        # 자본은 평가액(positions + cash) 사용
        equity = account_snap.total_evaluation_krw
        if equity <= 0:
            equity = account_snap.cash_krw  # 폴백
        sizing_inputs = SizingInputs(
            symbol=signal.symbol,
            side=side_str,
            instrument_type=sym.instrument_type.value,
            reference_price=ref_price,
            equity_krw=equity,
            available_cash_krw=account_snap.available_cash_krw,
            used_per_day_krw=Decimal("0"),  # v0.1: 미추적 — Task 25 P&L에서 계산
            strategy_id=signal.strategy_id,
        )
        sizing_result = self._sizer.size(sizing_inputs)
        stage_results["sizing"] = sizing_result.decision

        if sizing_result.decision == "reject":
            return self._finalize(
                execution_id, signal_id, signal,
                outcome=ExecutionOutcome.REJECTED,
                final_stage=ExecutionStage.SIZING,
                reject_reason=f"sizing rejected: {sizing_result.reject_reason}",
                started_at=started_at,
                stage_results=stage_results,
                metadata=metadata,
                aligned_price=sizing_result.aligned_price,
                quantity=sizing_result.quantity,
                estimated_cost_krw=sizing_result.estimated_cost_krw,
            )

        # Stop-first 재점검
        sd = self._shutdown.check()
        if sd.active:
            return self._finalize(
                execution_id, signal_id, signal,
                outcome=ExecutionOutcome.REJECTED,
                final_stage=ExecutionStage.STOP_CHECK,
                reject_reason=f"shutdown active: {sd.reason}",
                started_at=started_at,
                stage_results=stage_results, metadata=metadata,
            )

        # ───────────────── [6] RISK GATE (Task 19 v0.4) ─────────────────
        # 미체결 주문 조회
        pending_orders = self._adapter.orders.fetch_open_orders()

        risk_ctx = RiskContext(
            symbol=signal.symbol,
            side=side_str,
            quantity=sizing_result.quantity,
            price=sizing_result.aligned_price,
            estimated_cost_krw=sizing_result.estimated_cost_krw,
            strategy_id=signal.strategy_id,
            intent_id=execution_id,
            instrument_type=sym.instrument_type.value,
            equity_krw=equity,
            available_cash_krw=account_snap.available_cash_krw,
            daily_realized_pnl_krw=daily_realized_pnl_krw,
            open_positions=account_snap.open_positions_dict(),
            pending_orders=pending_orders,
            market_now_utc=datetime.now(timezone.utc),
            market_is_open=market_is_open,
            last_quote_price=quote_snap.last_trade_price or ref_price,
            last_order_at_utc=last_order_at_utc,
            last_order_for_symbol_utc=last_order_for_symbol_utc,
        )

        risk_decision = self._risk.run(risk_ctx)
        stage_results["risk_gate"] = risk_decision.outcome

        if risk_decision.outcome == "reject":
            return self._finalize(
                execution_id, signal_id, signal,
                outcome=ExecutionOutcome.REJECTED,
                final_stage=ExecutionStage.RISK_GATE,
                reject_reason=f"risk gate: {risk_decision.first_reject_reason}",
                started_at=started_at,
                stage_results=stage_results,
                metadata=metadata,
                aligned_price=sizing_result.aligned_price,
                quantity=sizing_result.quantity,
                estimated_cost_krw=sizing_result.estimated_cost_krw,
            )

        # Stop-first 재점검
        sd = self._shutdown.check()
        if sd.active:
            return self._finalize(
                execution_id, signal_id, signal,
                outcome=ExecutionOutcome.REJECTED,
                final_stage=ExecutionStage.STOP_CHECK,
                reject_reason=f"shutdown active: {sd.reason}",
                started_at=started_at,
                stage_results=stage_results, metadata=metadata,
            )

        # ───────────────── [7] APPROVAL ─────────────────
        approval_req = ApprovalRequest(
            execution_id=execution_id,
            signal_id=signal_id,
            symbol=signal.symbol,
            side=side_str,
            quantity=sizing_result.quantity,
            price=sizing_result.aligned_price,
            estimated_cost_krw=sizing_result.estimated_cost_krw,
            is_dry_run=not self._adapter.dry_run_guard.live_enabled,
            is_live_env=self._adapter.env.value == "live",
            requested_at_utc=datetime.now(timezone.utc),
            metadata={"signal_score": str(signal.composite_score)},
        )
        try:
            approval = self._approval.request_approval(approval_req)
        except Exception as e:  # noqa: BLE001
            logger.exception("Approval 오류")
            return self._finalize(
                execution_id, signal_id, signal,
                outcome=ExecutionOutcome.ERROR,
                final_stage=ExecutionStage.APPROVAL,
                reject_reason=f"approval error: {type(e).__name__}: {e}",
                started_at=started_at,
                stage_results=stage_results, metadata=metadata,
                aligned_price=sizing_result.aligned_price,
                quantity=sizing_result.quantity,
                estimated_cost_krw=sizing_result.estimated_cost_krw,
            )

        stage_results["approval"] = "approved" if approval.approved else "denied"
        metadata["approver"] = approval.approver

        if not approval.approved:
            return self._finalize(
                execution_id, signal_id, signal,
                outcome=ExecutionOutcome.REJECTED,
                final_stage=ExecutionStage.APPROVAL,
                reject_reason=f"approval denied: {approval.reason}",
                started_at=started_at,
                stage_results=stage_results,
                metadata=metadata,
                aligned_price=sizing_result.aligned_price,
                quantity=sizing_result.quantity,
                estimated_cost_krw=sizing_result.estimated_cost_krw,
            )

        # Stop-first 최종 점검 (주문 직전)
        sd = self._shutdown.check()
        if sd.active:
            return self._finalize(
                execution_id, signal_id, signal,
                outcome=ExecutionOutcome.REJECTED,
                final_stage=ExecutionStage.STOP_CHECK,
                reject_reason=f"shutdown active (pre-submission): {sd.reason}",
                started_at=started_at,
                stage_results=stage_results, metadata=metadata,
                aligned_price=sizing_result.aligned_price,
                quantity=sizing_result.quantity,
                estimated_cost_krw=sizing_result.estimated_cost_krw,
            )

        # ───────────────── [8] SUBMISSION ─────────────────
        order_req = OrderRequest(
            symbol=signal.symbol,
            side=OrderSide(side_str),
            quantity=sizing_result.quantity,
            order_type=OrderType.LIMIT,
            limit_price=sizing_result.aligned_price,
            client_order_id=execution_id,  # 멱등 키
        )
        try:
            order_resp = self._adapter.orders.submit_order(order_req)
        except Exception as e:  # noqa: BLE001
            logger.exception("주문 송신 오류")
            return self._finalize(
                execution_id, signal_id, signal,
                outcome=ExecutionOutcome.ERROR,
                final_stage=ExecutionStage.SUBMISSION,
                reject_reason=f"submit error: {type(e).__name__}: {e}",
                started_at=started_at,
                stage_results=stage_results, metadata=metadata,
                aligned_price=sizing_result.aligned_price,
                quantity=sizing_result.quantity,
                estimated_cost_krw=sizing_result.estimated_cost_krw,
            )

        stage_results["submission"] = "accepted" if order_resp.accepted else "rejected"

        if not order_resp.accepted:
            return self._finalize(
                execution_id, signal_id, signal,
                outcome=ExecutionOutcome.REJECTED,
                final_stage=ExecutionStage.SUBMISSION,
                reject_reason=f"broker rejected: {order_resp.error}",
                started_at=started_at,
                stage_results=stage_results,
                metadata=metadata,
                is_dry_run=order_resp.is_dry_run,
                aligned_price=sizing_result.aligned_price,
                quantity=sizing_result.quantity,
                estimated_cost_krw=sizing_result.estimated_cost_krw,
            )

        # 성공
        return self._finalize(
            execution_id, signal_id, signal,
            outcome=ExecutionOutcome.SUBMITTED,
            final_stage=ExecutionStage.DONE,
            reject_reason=None,
            started_at=started_at,
            stage_results=stage_results,
            metadata=metadata,
            is_dry_run=order_resp.is_dry_run,
            broker_order_no=order_resp.broker_order_no,
            aligned_price=sizing_result.aligned_price,
            quantity=sizing_result.quantity,
            estimated_cost_krw=sizing_result.estimated_cost_krw,
        )

    # ------------------------------------------------------------------
    # 결과 마무리 (audit log + ExecutionResult 반환)
    # ------------------------------------------------------------------

    def _finalize(
        self,
        execution_id: str,
        signal_id: str,
        signal: MomentumSignalV04,
        *,
        outcome: ExecutionOutcome,
        final_stage: ExecutionStage,
        reject_reason: Optional[str],
        started_at: datetime,
        stage_results: dict[str, Any],
        metadata: dict[str, Any],
        is_dry_run: Optional[bool] = None,
        broker_order_no: Optional[str] = None,
        quantity: Optional[int] = None,
        aligned_price: Optional[Decimal] = None,
        estimated_cost_krw: Optional[Decimal] = None,
    ) -> ExecutionResult:
        completed_at = datetime.now(timezone.utc)

        record = build_record(
            execution_id=execution_id,
            signal_id=signal_id,
            symbol=signal.symbol,
            strategy_id=signal.strategy_id,
            side=signal.side.value,
            outcome=outcome,
            final_stage=final_stage,
            started_at_utc=started_at,
            completed_at_utc=completed_at,
            reject_reason=reject_reason,
            is_dry_run=is_dry_run,
            broker_order_no=broker_order_no,
            quantity=quantity,
            aligned_price=aligned_price,
            estimated_cost_krw=estimated_cost_krw,
            stage_results=stage_results,
            metadata=metadata,
        )
        self._audit.write(record)

        logger.info(
            "[gateway] 종료: execution_id=%s outcome=%s final_stage=%s reason=%s",
            execution_id, outcome.value, final_stage.value, reject_reason,
        )

        return ExecutionResult(
            execution_id=execution_id,
            signal_id=signal_id,
            outcome=outcome,
            final_stage=final_stage,
            reject_reason=reject_reason,
            is_dry_run=is_dry_run,
            broker_order_no=broker_order_no,
            quantity=quantity,
            aligned_price=aligned_price,
            estimated_cost_krw=estimated_cost_krw,
            started_at_utc=started_at,
            completed_at_utc=completed_at,
            metadata=metadata,
        )
