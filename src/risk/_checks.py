"""사전 리스크 게이트 검사 함수 (9개) — _checks.py

risk_limits.yaml §10.1 evaluation_order 와 정합.

각 검사는 순수 함수: ctx (RiskGateContext) → CheckResult.
모든 검사는 try/except 로 감싸여 있으며, 예외 발생 시 fail-closed 거부.

검사 목록:
1. check_kill_switch         — Task 31 KillSwitchMonitor
2. check_emergency_stop      — Task 29-30 StopState
3. check_market_state        — Task 11 KrxCalendar (CLOSED, NEAR_CLOSE 등)
4. check_capacity            — capacity.yaml (per_order, per_symbol exposure)
5. check_loss_limits         — risk_limits.loss_limits (daily/session/trailing/per-pos)
6. check_position_limits     — risk_limits.position_limits
7. check_order_frequency     — risk_limits.order_frequency_limits
8. check_duplicate_conflict  — risk_limits.duplicate_conflict_guards
9. check_execution_guards    — risk_limits.execution_guards
"""
from __future__ import annotations
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from src.brokers import OrderType, Side
from src.data import MarketPhase

from ._context import RiskGateContext
from ._decision import CheckResult, RejectionReason


# ============================================================
# 헬퍼 (Helpers)
# ============================================================
def _safe_decimal(value: Any) -> Optional[Decimal]:
    """dict 에서 읽은 값을 Decimal 로 변환, 실패 시 None."""
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _get_nested(d: dict, *keys: str) -> Optional[Any]:
    """안전한 nested dict 접근. 어떤 키든 누락 시 None."""
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def _estimate_notional(ctx: RiskGateContext) -> Optional[Decimal]:
    """주문의 명목금액 추정.

    - LIMIT: limit_price * quantity
    - MARKET: ctx.quote.ask (BUY) 또는 ctx.quote.bid (SELL) * quantity
              quote 없으면 None.
    """
    qty = Decimal(ctx.intent.quantity)
    if ctx.intent.order_type == OrderType.LIMIT:
        if ctx.intent.limit_price is None:
            return None
        return ctx.intent.limit_price * qty
    # MARKET
    if ctx.quote is None:
        return None
    price = ctx.quote.ask if ctx.intent.side == Side.BUY else ctx.quote.bid
    return price * qty


# ============================================================
# 1. Kill Switch (가장 먼저)
# ============================================================
def check_kill_switch(ctx: RiskGateContext) -> CheckResult:
    """Kill switch 파일 검사 — risk_limits.yaml §9.1
    check_before_every_order: true 정합.

    본 검사는 *kill switch 파일의 실제 존재 여부* 만 본다.
    StopState 가 이미 stopped 인 경우는 다음 검사(check_emergency_stop)가
    EMERGENCY_STOP 사유로 분류한다 — 사유 분류 명확성을 위해 분리.

    파일 시스템 검사 실패는 KillSwitchMonitor.file_present() 가 fail-safe
    로 True 반환하므로 본 검사도 KILL_SWITCH 거부로 처리.
    """
    try:
        if ctx.kill_switch.file_present():
            # 파일이 존재 — KillSwitchMonitor 를 통해 정지 발동도 함께
            ctx.kill_switch.check_now()
            return CheckResult.reject(
                "check_kill_switch",
                RejectionReason.KILL_SWITCH,
                detail="kill switch file present — new orders blocked",
            )
        return CheckResult.pass_("check_kill_switch")
    except Exception as e:
        return CheckResult.reject(
            "check_kill_switch",
            RejectionReason.VALIDATION_ERROR,
            detail=f"kill switch check failed: {type(e).__name__}",
        )


# ============================================================
# 2. Emergency Stop (StopState)
# ============================================================
def check_emergency_stop(ctx: RiskGateContext) -> CheckResult:
    """비상 정지 상태 검사 — Task 29-30 통합.

    SIGINT, ESC 키, KILL_SWITCH 등 모든 정지 입력이 통합되어 있음.
    """
    try:
        if ctx.stop_state.is_stopped():
            ev = ctx.stop_state.first_event()
            stop_reason = ev.reason.value if ev else "unknown"
            return CheckResult.reject(
                "check_emergency_stop",
                RejectionReason.EMERGENCY_STOP,
                detail=f"stop active: {stop_reason}",
                stop_reason=stop_reason,
            )
        return CheckResult.pass_("check_emergency_stop")
    except Exception as e:
        return CheckResult.reject(
            "check_emergency_stop",
            RejectionReason.VALIDATION_ERROR,
            detail=f"stop state check failed: {type(e).__name__}",
        )


# ============================================================
# 3. Market State (Calendar)
# ============================================================
def check_market_state(ctx: RiskGateContext) -> CheckResult:
    """시장 상태 검사 — Task 11 KrxCalendar.

    CLOSED → MARKET_CLOSED
    PRE_OPEN, NEAR_CLOSE, AFTER_HOURS → MARKET_STATE_GUARD (현재 거래 비허용)
    REGULAR → PASS
    """
    try:
        require_open = _get_nested(
            ctx.risk_limits_config, "market_state_guards", "require_market_open"
        )
        require_open = True if require_open is None else bool(require_open)

        phase = ctx.calendar.get_phase(ctx.now_utc)

        if phase == MarketPhase.REGULAR:
            return CheckResult.pass_("check_market_state", phase=phase.value)

        if phase == MarketPhase.CLOSED:
            if require_open:
                return CheckResult.reject(
                    "check_market_state",
                    RejectionReason.MARKET_CLOSED,
                    detail="market is closed (holiday or off-hours)",
                    phase=phase.value,
                )
            return CheckResult.pass_("check_market_state", phase=phase.value)

        # PRE_OPEN, NEAR_CLOSE, AFTER_HOURS — 본 시스템 Phase 1 미허용
        return CheckResult.reject(
            "check_market_state",
            RejectionReason.MARKET_STATE_GUARD,
            detail=f"market phase {phase.value} — orders not allowed in this phase",
            phase=phase.value,
        )
    except Exception as e:
        return CheckResult.reject(
            "check_market_state",
            RejectionReason.VALIDATION_ERROR,
            detail=f"market state check failed: {type(e).__name__}",
        )


# ============================================================
# 4. Capacity (capacity.yaml)
# ============================================================
def check_capacity(ctx: RiskGateContext) -> CheckResult:
    """capacity.yaml 한도 검사.

    - per_order_max_notional
    - per_symbol_max_exposure (기존 포지션 + 신규 주문)
    - minimum_cash_reserve (BUY 주문일 때만)
    """
    try:
        per_order_max = _safe_decimal(
            _get_nested(ctx.capacity_config, "capital_caps",
                        "per_order_max_notional", "amount")
        )
        per_symbol_max = _safe_decimal(
            _get_nested(ctx.capacity_config, "capital_caps",
                        "per_symbol_max_exposure", "amount")
        )

        if per_order_max is None or per_symbol_max is None:
            return CheckResult.reject(
                "check_capacity",
                RejectionReason.VALIDATION_ERROR,
                detail="capacity config missing per_order/per_symbol caps",
            )

        notional = _estimate_notional(ctx)
        if notional is None:
            return CheckResult.reject(
                "check_capacity",
                RejectionReason.VALIDATION_ERROR,
                detail="cannot estimate notional (missing quote for MARKET, or invalid LIMIT)",
            )

        # 4.1 단일 주문 한도
        if notional > per_order_max:
            return CheckResult.reject(
                "check_capacity",
                RejectionReason.CAPACITY_BREACH,
                detail=f"notional {notional} exceeds per_order_max {per_order_max}",
                notional=str(notional), threshold=str(per_order_max),
            )

        # 4.2 심볼별 노출 한도 (BUY 만 추가; SELL 은 청산 방향)
        if ctx.intent.side == Side.BUY:
            existing = sum(
                (p.market_value for p in ctx.positions
                 if p.symbol == ctx.intent.symbol),
                Decimal(0),
            )
            projected = existing + notional
            if projected > per_symbol_max:
                return CheckResult.reject(
                    "check_capacity",
                    RejectionReason.CAPACITY_BREACH,
                    detail=f"projected exposure {projected} > per_symbol_max {per_symbol_max}",
                    existing=str(existing), notional=str(notional),
                    projected=str(projected), threshold=str(per_symbol_max),
                )

        # 4.3 최소 현금 (BUY 만 적용)
        min_cash_pct = _safe_decimal(
            _get_nested(ctx.capacity_config, "capital_caps",
                        "minimum_cash_reserve", "percentage")
        )
        min_cash_floor = _safe_decimal(
            _get_nested(ctx.capacity_config, "capital_caps",
                        "minimum_cash_reserve", "floor_amount")
        )
        if ctx.intent.side == Side.BUY and \
           min_cash_pct is not None and min_cash_floor is not None:
            cash_after = ctx.cash_balance - notional
            min_cash_required = max(
                ctx.total_equity * min_cash_pct / Decimal(100),
                min_cash_floor,
            )
            if cash_after < min_cash_required:
                return CheckResult.reject(
                    "check_capacity",
                    RejectionReason.CAPACITY_BREACH,
                    detail=f"cash after order {cash_after} < min reserve {min_cash_required}",
                    cash_after=str(cash_after),
                    min_cash_required=str(min_cash_required),
                )

        return CheckResult.pass_(
            "check_capacity",
            notional=str(notional),
        )
    except Exception as e:
        return CheckResult.reject(
            "check_capacity",
            RejectionReason.VALIDATION_ERROR,
            detail=f"capacity check failed: {type(e).__name__}: {e}",
        )


# ============================================================
# 5. Loss Limits
# ============================================================
def check_loss_limits(ctx: RiskGateContext) -> CheckResult:
    """risk_limits.yaml §3 loss_limits 검사.

    - daily_loss_cap (realized + unrealized)
    - session_loss_cap (realized + unrealized)
    - trailing_drawdown (vs session_high_equity)
    """
    try:
        # 손실 측정 (음수일수록 손실 큼)
        total_pnl = ctx.session_realized_pnl + ctx.session_unrealized_pnl

        # 5.1 daily_loss_cap (절대 금액 — yaml 의 amount 는 손실 한도, 양수)
        daily_cap = _safe_decimal(
            _get_nested(ctx.risk_limits_config, "loss_limits",
                        "daily_loss_cap", "amount")
        )
        if daily_cap is not None and total_pnl <= -daily_cap:
            return CheckResult.reject(
                "check_loss_limits",
                RejectionReason.LOSS_LIMIT_BREACH,
                detail=f"daily loss cap reached: pnl={total_pnl}, cap={daily_cap}",
                pnl=str(total_pnl), cap=str(daily_cap), kind="daily",
            )

        # 5.2 session_loss_cap
        session_cap = _safe_decimal(
            _get_nested(ctx.risk_limits_config, "loss_limits",
                        "session_loss_cap", "amount")
        )
        if session_cap is not None and total_pnl <= -session_cap:
            return CheckResult.reject(
                "check_loss_limits",
                RejectionReason.LOSS_LIMIT_BREACH,
                detail=f"session loss cap reached: pnl={total_pnl}, cap={session_cap}",
                pnl=str(total_pnl), cap=str(session_cap), kind="session",
            )

        # 5.3 trailing_drawdown
        td_enabled = _get_nested(ctx.risk_limits_config, "loss_limits",
                                 "trailing_drawdown", "enabled")
        td_pct = _safe_decimal(
            _get_nested(ctx.risk_limits_config, "loss_limits",
                        "trailing_drawdown", "threshold_pct")
        )
        if td_enabled and td_pct is not None and ctx.session_high_equity > 0:
            current_equity = ctx.total_equity + ctx.session_unrealized_pnl
            drawdown_pct = (
                (ctx.session_high_equity - current_equity)
                / ctx.session_high_equity * Decimal(100)
            )
            if drawdown_pct >= td_pct:
                return CheckResult.reject(
                    "check_loss_limits",
                    RejectionReason.LOSS_LIMIT_BREACH,
                    detail=f"trailing drawdown {drawdown_pct:.2f}% >= {td_pct}%",
                    drawdown_pct=str(drawdown_pct), threshold_pct=str(td_pct),
                    kind="trailing_drawdown",
                )

        return CheckResult.pass_("check_loss_limits", pnl=str(total_pnl))
    except Exception as e:
        return CheckResult.reject(
            "check_loss_limits",
            RejectionReason.VALIDATION_ERROR,
            detail=f"loss limits check failed: {type(e).__name__}: {e}",
        )


# ============================================================
# 6. Position Limits
# ============================================================
def check_position_limits(ctx: RiskGateContext) -> CheckResult:
    """risk_limits.yaml §4 position_limits 검사."""
    try:
        max_concurrent = _get_nested(ctx.risk_limits_config, "position_limits",
                                     "max_concurrent_positions")
        max_open = _get_nested(ctx.risk_limits_config, "position_limits",
                               "max_open_orders")
        max_qty = _get_nested(ctx.risk_limits_config, "position_limits",
                              "max_quantity_per_order")

        if max_concurrent is None or max_open is None or max_qty is None:
            return CheckResult.reject(
                "check_position_limits",
                RejectionReason.VALIDATION_ERROR,
                detail="position_limits config incomplete",
            )

        # 6.1 max_quantity_per_order
        if ctx.intent.quantity > int(max_qty):
            return CheckResult.reject(
                "check_position_limits",
                RejectionReason.POSITION_LIMIT_BREACH,
                detail=f"quantity {ctx.intent.quantity} > max {max_qty}",
                quantity=ctx.intent.quantity, threshold=int(max_qty),
            )

        # 6.2 max_open_orders
        if ctx.open_orders_count >= int(max_open):
            return CheckResult.reject(
                "check_position_limits",
                RejectionReason.POSITION_LIMIT_BREACH,
                detail=f"open orders {ctx.open_orders_count} >= max {max_open}",
                open_orders=ctx.open_orders_count, threshold=int(max_open),
            )

        # 6.3 max_concurrent_positions (BUY 가 신규 심볼이면 +1)
        if ctx.intent.side == Side.BUY:
            symbols_with_pos = {p.symbol for p in ctx.positions}
            new_position = ctx.intent.symbol not in symbols_with_pos
            projected = len(symbols_with_pos) + (1 if new_position else 0)
            if projected > int(max_concurrent):
                return CheckResult.reject(
                    "check_position_limits",
                    RejectionReason.POSITION_LIMIT_BREACH,
                    detail=f"projected positions {projected} > max {max_concurrent}",
                    current=len(symbols_with_pos), projected=projected,
                    threshold=int(max_concurrent),
                )

        return CheckResult.pass_("check_position_limits")
    except Exception as e:
        return CheckResult.reject(
            "check_position_limits",
            RejectionReason.VALIDATION_ERROR,
            detail=f"position limits check failed: {type(e).__name__}: {e}",
        )


# ============================================================
# 7. Order Frequency
# ============================================================
def check_order_frequency(ctx: RiskGateContext) -> CheckResult:
    """risk_limits.yaml §5 order_frequency_limits 검사.

    분당/5분/시간/세션 윈도우의 주문 수 한도.
    history.count_in_window 사용.
    """
    try:
        per_min = _get_nested(ctx.risk_limits_config, "order_frequency_limits",
                              "max_orders_per_minute")
        per_5m = _get_nested(ctx.risk_limits_config, "order_frequency_limits",
                             "max_orders_per_5_minutes")
        per_hour = _get_nested(ctx.risk_limits_config, "order_frequency_limits",
                               "max_orders_per_hour")
        per_session = _get_nested(ctx.risk_limits_config, "order_frequency_limits",
                                  "max_orders_per_session")

        if any(x is None for x in (per_min, per_5m, per_hour, per_session)):
            return CheckResult.reject(
                "check_order_frequency",
                RejectionReason.VALIDATION_ERROR,
                detail="order_frequency_limits config incomplete",
            )

        # 윈도우별 검사 (오름차순; 가장 짧은 것이 더 strict)
        windows = [
            ("per_minute", 60, int(per_min)),
            ("per_5_minutes", 300, int(per_5m)),
            ("per_hour", 3600, int(per_hour)),
            # 세션은 전체 history 길이로 근사 (세션 시작 이후 모든 주문)
        ]
        for label, sec, threshold in windows:
            count = ctx.history.count_in_window(ctx.now_utc, sec)
            if count >= threshold:
                return CheckResult.reject(
                    "check_order_frequency",
                    RejectionReason.ORDER_FREQUENCY_BREACH,
                    detail=f"{label}: {count} >= {threshold}",
                    window=label, count=count, threshold=threshold,
                )

        # 세션 한도 (history 전체 길이)
        if len(ctx.history) >= int(per_session):
            return CheckResult.reject(
                "check_order_frequency",
                RejectionReason.ORDER_FREQUENCY_BREACH,
                detail=f"session: {len(ctx.history)} >= {per_session}",
                window="session", count=len(ctx.history),
                threshold=int(per_session),
            )

        return CheckResult.pass_("check_order_frequency")
    except Exception as e:
        return CheckResult.reject(
            "check_order_frequency",
            RejectionReason.VALIDATION_ERROR,
            detail=f"order frequency check failed: {type(e).__name__}: {e}",
        )


# ============================================================
# 8. Duplicate / Conflict Guards
# ============================================================
def check_duplicate_conflict(ctx: RiskGateContext) -> CheckResult:
    """risk_limits.yaml §7 duplicate_conflict_guards 검사.

    - no_duplicate_while_pending (윈도우 내 동일 심볼·동일 사이드 차단)
    - self_cross_prevention (같은 시점 BUY+SELL 의도 차단; 이력 기반 근사)
    - no_immediate_reversal / whipsaw_guard
    """
    try:
        # 8.1 중복 주문 검사
        no_dup = _get_nested(ctx.risk_limits_config,
                             "duplicate_conflict_guards", "no_duplicate_while_pending")
        dup_window = _get_nested(ctx.risk_limits_config,
                                 "duplicate_conflict_guards", "duplicate_window_seconds")
        if no_dup and dup_window is not None:
            if ctx.history.has_duplicate(
                ctx.intent, ctx.now_utc, int(dup_window)
            ):
                return CheckResult.reject(
                    "check_duplicate_conflict",
                    RejectionReason.DUPLICATE_ORDER,
                    detail=f"duplicate within {dup_window}s window",
                    window_seconds=int(dup_window),
                )

        # 8.2 self-cross prevention (현재 보유와 반대 방향이지만 큰 수량 등)
        # Phase 1 단순화: 같은 심볼에 BUY/SELL 모두 미체결인 경우만 검출
        # — 이는 위 8.1 의 "동일 사이드" 와는 다름 (반대 사이드)
        self_cross = _get_nested(ctx.risk_limits_config,
                                 "duplicate_conflict_guards", "self_cross_prevention")
        if self_cross:
            # cooldown_after_fill 윈도우 내 반대 사이드 검사를 8.3 와 합쳐 처리
            pass  # 8.3 의 has_recent_opposite_side 로 사실상 커버됨

        # 8.3 whipsaw guard (반대 방향 즉시 진입 차단)
        no_reversal = _get_nested(ctx.risk_limits_config,
                                  "duplicate_conflict_guards", "no_immediate_reversal")
        reversal_cd = _get_nested(ctx.risk_limits_config,
                                  "duplicate_conflict_guards", "reversal_cooldown_seconds")
        if no_reversal and reversal_cd is not None:
            if ctx.history.has_recent_opposite_side(
                ctx.intent, ctx.now_utc, int(reversal_cd)
            ):
                return CheckResult.reject(
                    "check_duplicate_conflict",
                    RejectionReason.WHIPSAW_GUARD,
                    detail=f"opposite-side order within {reversal_cd}s",
                    cooldown_seconds=int(reversal_cd),
                )

        return CheckResult.pass_("check_duplicate_conflict")
    except Exception as e:
        return CheckResult.reject(
            "check_duplicate_conflict",
            RejectionReason.VALIDATION_ERROR,
            detail=f"duplicate/conflict check failed: {type(e).__name__}: {e}",
        )


# ============================================================
# 9. Execution Guards
# ============================================================
def check_execution_guards(ctx: RiskGateContext) -> CheckResult:
    """risk_limits.yaml §6 execution_guards 검사.

    - bid_ask_spread_cap
    - price_sanity (LIMIT 주문이 last 대비 너무 벗어남)
    - quote_freshness (quote 의 ts 가 현재로부터 너무 오래됨)
    - 슬리피지(slippage)는 발주 시점 측정이라 본 게이트에서 직접 비교 어려움;
      arrival_price 기반 비교는 발주 결과 평가 단계 (Task 27 slippage)에서 수행.
      게이트에서는 LIMIT 가격이 last 대비 spread 안에 있는지 정도만 판단.
    """
    try:
        # quote 가 없으면 이 검사들은 스킵 가능 — 단, MARKET 주문에는
        # 이미 capacity_check 에서 quote 필수 검증됨
        if ctx.quote is None:
            return CheckResult.pass_("check_execution_guards",
                                     note="quote unavailable, skipped")

        q = ctx.quote

        # 9.1 quote freshness
        max_age = _safe_decimal(
            _get_nested(ctx.risk_limits_config, "execution_guards",
                        "quote_freshness", "max_age_seconds")
        )
        if max_age is not None:
            age_seconds = (ctx.now_utc - q.ts).total_seconds()
            if Decimal(str(age_seconds)) > max_age:
                return CheckResult.reject(
                    "check_execution_guards",
                    RejectionReason.QUOTE_STALE,
                    detail=f"quote age {age_seconds:.1f}s > {max_age}s",
                    age_seconds=str(age_seconds), threshold=str(max_age),
                )

        # 9.2 spread cap
        max_spread_bps = _safe_decimal(
            _get_nested(ctx.risk_limits_config, "execution_guards",
                        "bid_ask_spread_cap", "max_bps")
        )
        if max_spread_bps is not None:
            spread = q.spread_bps
            if spread is not None and spread > max_spread_bps:
                return CheckResult.reject(
                    "check_execution_guards",
                    RejectionReason.SPREAD_TOO_WIDE,
                    detail=f"spread {spread:.1f}bps > {max_spread_bps}bps",
                    spread_bps=str(spread), threshold_bps=str(max_spread_bps),
                )

        # 9.3 price sanity (LIMIT 주문일 때만)
        if ctx.intent.order_type == OrderType.LIMIT and ctx.intent.limit_price is not None:
            max_dev_pct = _safe_decimal(
                _get_nested(ctx.risk_limits_config, "execution_guards",
                            "price_sanity", "max_deviation_from_last_pct")
            )
            if max_dev_pct is not None and q.last > 0:
                dev_pct = abs(ctx.intent.limit_price - q.last) / q.last * Decimal(100)
                if dev_pct > max_dev_pct:
                    return CheckResult.reject(
                        "check_execution_guards",
                        RejectionReason.PRICE_SANITY_FAIL,
                        detail=f"limit_price deviates {dev_pct:.2f}% from last (max {max_dev_pct}%)",
                        deviation_pct=str(dev_pct), threshold_pct=str(max_dev_pct),
                    )

        return CheckResult.pass_("check_execution_guards")
    except Exception as e:
        return CheckResult.reject(
            "check_execution_guards",
            RejectionReason.VALIDATION_ERROR,
            detail=f"execution guards check failed: {type(e).__name__}: {e}",
        )
