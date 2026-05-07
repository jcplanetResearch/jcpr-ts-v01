"""
MCP Restricted 서버 (MCP Restricted Server)
=============================================

JCPR Trading System - jcpr-ts-v01
Task 35 v0.1

Write 도구를 제공하되, 모든 도구는 인간 승인 게이트 통과 필수.
(Provides write tools but all require human approval gate.)

핵심 보안 (Critical Security):
    1. Agent 단독 실행 금지 — request → 승인 → execute 3단계
    2. self-approval 차단 (operator_id != requested_by)
    3. paper-only 강제 (allow_live=True 명시 + mode='live' 명시 시만)
    4. 모든 호출 audit
    5. stdio 전용
    6. 도메인 실행은 stub (Task 21 미통합)

내부 도구 (CLI 전용 — MCP 노출 안 함):
    - approve_action / reject_action — scripts/approve_cli.py 가 호출
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from src.observability import (
    AuditWriter,
    ORIGIN_AGENT,
    TraceContext,
    configure_default_writer,
    get_default_writer,
)

from ._approval_store import ApprovalStore
from ._config import RestrictedServerConfig, load_restricted_config_from_env
from ._security import (
    RateLimiter,
    check_result_size,
)
from . import _write_handlers as wh


logger = logging.getLogger("jcpr.mcp.restricted")
_handler_set = False


def _setup_logging() -> None:
    global _handler_set
    if _handler_set:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)s %(name)s: %(message)s"
    ))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    _handler_set = True


# ─────────────────────────────────────────────────
# Trace + Audit 헬퍼
# ─────────────────────────────────────────────────

def _make_tool_trace(
    config: RestrictedServerConfig,
    tool_name: str,
    correlation: Optional[dict] = None,
) -> TraceContext:
    keys: dict[str, Any] = {
        "tool": tool_name,
        "server": "restricted_mcp",
    }
    if correlation:
        keys.update(correlation)
    return TraceContext.new(
        origin=ORIGIN_AGENT,
        operator_id="mcp_client",
        session_id=config.session_id,
        correlation_keys=keys,
    )


def _audit_call_start(
    writer: Optional[AuditWriter],
    ctx: TraceContext,
    tool_name: str,
    args: dict,
) -> None:
    if writer is None:
        return
    writer.write_mcp_call(ctx, payload={"tool": tool_name, "args": args})


def _audit_call_result(
    writer: Optional[AuditWriter],
    ctx: TraceContext,
    tool_name: str,
    result: dict,
) -> None:
    if writer is None:
        return
    summary = {
        "tool": tool_name,
        "ok": result.get("ok"),
        "approval_id": result.get("approval_id"),
        "status": result.get("status"),
    }
    if not result.get("ok"):
        summary["error_code"] = result.get("error_code")
        summary["error_message"] = result.get("error_message", "")[:200]
    writer.write_mcp_result(ctx, payload=summary)


def _audit_approval_event(
    writer: Optional[AuditWriter],
    ctx: TraceContext,
    event_type: str,
    result: dict,
) -> None:
    """승인 관련 audit (approval_request/approval_decision)."""
    if writer is None or not result.get("ok"):
        return
    payload = {
        "approval_id": result.get("approval_id"),
        "action_type": result.get("action_type"),
        "status": result.get("status"),
        "requested_by": result.get("requested_by"),
        "decided_by": result.get("decided_by"),
    }
    if event_type == "approval_request":
        writer.write_approval_request(ctx, payload=payload)
    elif event_type == "approval_decision":
        writer.write_approval_decision(ctx, payload=payload)


# ─────────────────────────────────────────────────
# Wrapper (rate limit + audit + size check)
# ─────────────────────────────────────────────────

def _wrap_call(
    config: RestrictedServerConfig,
    rate_limiter: RateLimiter,
    tool_name: str,
    handler_fn,
    args: dict,
    *,
    audit_event_type: Optional[str] = None,
) -> dict:
    """공통 wrapper: rate limit → audit → handler → audit → size check."""
    writer = get_default_writer()
    ctx = _make_tool_trace(config, tool_name, correlation={
        "args_keys": list(args.keys()),
    })

    ok, err = rate_limiter.check()
    if not ok:
        result = {
            "ok": False,
            "error_code": "RATE_LIMIT",
            "error_message": err,
        }
        _audit_call_result(writer, ctx, tool_name, result)
        return result

    _audit_call_start(writer, ctx, tool_name, args)

    try:
        result = handler_fn(**args)
    except TypeError as e:
        result = {
            "ok": False, "error_code": "INVALID_ARGS",
            "error_message": str(e),
        }
    except Exception as e:  # noqa: BLE001
        if writer is not None:
            writer.write_exception(ctx, e, additional={"tool": tool_name})
        result = {
            "ok": False, "error_code": "HANDLER_ERROR",
            "error_message": f"{type(e).__name__}: {e}",
        }

    # 크기 검증
    try:
        s = json.dumps(result, ensure_ascii=False, default=str)
        ok_size, msg = check_result_size(s)
        if not ok_size:
            result = {
                "ok": False, "error_code": "RESULT_TOO_LARGE",
                "error_message": msg,
            }
    except Exception as e:  # noqa: BLE001
        result = {
            "ok": False, "error_code": "SERIALIZATION_ERROR",
            "error_message": str(e),
        }

    _audit_call_result(writer, ctx, tool_name, result)
    if audit_event_type:
        _audit_approval_event(writer, ctx, audit_event_type, result)

    result["_trace_id"] = ctx.trace_id
    return result


# ─────────────────────────────────────────────────
# Server Builder
# ─────────────────────────────────────────────────

def build_server(
    config: Optional[RestrictedServerConfig] = None,
    *,
    store: Optional[ApprovalStore] = None,
) -> tuple[FastMCP, ApprovalStore]:
    """
    FastMCP restricted 서버 빌드.

    Args:
        config: 설정 (None이면 환경변수에서 로드)
        store: ApprovalStore 인스턴스 (None이면 config로 생성 — 테스트 편의)

    Returns:
        (FastMCP 인스턴스, ApprovalStore)
    """
    _setup_logging()

    if config is None:
        config = load_restricted_config_from_env()

    if get_default_writer() is None:
        configure_default_writer(config.audit_dir)
        logger.info(f"AuditWriter configured: {config.audit_dir}")

    if store is None:
        store = ApprovalStore(
            db_path=config.approval_db,
            default_ttl_seconds=config.approval_ttl_seconds,
            execute_ttl_seconds=config.execute_ttl_seconds,
            allow_self_approval=config.allow_self_approval,
        )

    rate_limiter = RateLimiter(max_per_minute=config.rate_limit_per_minute)

    mcp = FastMCP(
        name="jcpr-restricted",
        instructions=(
            "JCPR restricted MCP server. All write operations require "
            "human approval (3-phase: request → approve → execute). "
            "Default mode is paper-only. Live mode requires explicit "
            "config + mode='live' in payload."
        ),
    )

    # ─── Tool 1: request_submit_order ─────────
    @mcp.tool()
    def request_submit_order(
        symbol: str,
        side: str,
        qty: int,
        order_type: str = "market",
        price_krw: Optional[str] = None,
        mode: str = "paper",
        strategy_id: Optional[str] = None,
        client_order_id: Optional[str] = None,
        requested_by: str = "agent",
    ) -> dict:
        """
        Request order submission (REQUIRES HUMAN APPROVAL).

        Creates a pending approval. Operator must approve via CLI/dashboard
        before execute_approved_action can run.

        Args:
            symbol: Trading symbol (e.g. "005930")
            side: "buy" or "sell"
            qty: Quantity (positive integer)
            order_type: "market" or "limit"
            price_krw: Required if order_type="limit"
            mode: "paper" (default) or "live" (requires server config)
            strategy_id: Optional strategy attribution
            client_order_id: Optional idempotency token
            requested_by: Caller identity (agent name)

        Returns approval_id and pending status.
        """
        # 자동 trace_id (도구 호출별)
        ctx = _make_tool_trace(config, "request_submit_order")
        return _wrap_call(
            config, rate_limiter,
            "request_submit_order",
            lambda **kw: wh.request_submit_order(config, store, **kw),
            args={
                "symbol": symbol, "side": side, "qty": qty,
                "order_type": order_type, "price_krw": price_krw,
                "mode": mode, "strategy_id": strategy_id,
                "client_order_id": client_order_id,
                "requested_by": requested_by,
                "trace_id": ctx.trace_id,
                "parent_trace_id": None,
            },
            audit_event_type="approval_request",
        )

    # ─── Tool 2: request_cancel_order ─────────
    @mcp.tool()
    def request_cancel_order(
        order_id: str,
        reason: str = "",
        requested_by: str = "agent",
    ) -> dict:
        """
        Request order cancellation (REQUIRES HUMAN APPROVAL).

        Args:
            order_id: Existing order ID to cancel
            reason: Cancellation reason (≤500 chars)
            requested_by: Caller identity
        """
        ctx = _make_tool_trace(config, "request_cancel_order")
        return _wrap_call(
            config, rate_limiter,
            "request_cancel_order",
            lambda **kw: wh.request_cancel_order(config, store, **kw),
            args={
                "order_id": order_id, "reason": reason,
                "requested_by": requested_by,
                "trace_id": ctx.trace_id,
            },
            audit_event_type="approval_request",
        )

    # ─── Tool 3: request_set_capacity ─────────
    @mcp.tool()
    def request_set_capacity(
        capacity_krw: str,
        target: str = "total",
        strategy_id: Optional[str] = None,
        reason: str = "",
        requested_by: str = "agent",
    ) -> dict:
        """
        Request capacity limit change (REQUIRES HUMAN APPROVAL).

        Args:
            capacity_krw: New capacity as decimal string
            target: "total" or "per_strategy"
            strategy_id: Required if target="per_strategy"
            reason: Justification
            requested_by: Caller identity
        """
        ctx = _make_tool_trace(config, "request_set_capacity")
        return _wrap_call(
            config, rate_limiter,
            "request_set_capacity",
            lambda **kw: wh.request_set_capacity(config, store, **kw),
            args={
                "capacity_krw": capacity_krw,
                "target": target,
                "strategy_id": strategy_id,
                "reason": reason,
                "requested_by": requested_by,
                "trace_id": ctx.trace_id,
            },
            audit_event_type="approval_request",
        )

    # ─── Tool 4: request_kill_switch ──────────
    @mcp.tool()
    def request_kill_switch(
        activate: bool,
        reason: str,
        requested_by: str = "agent",
    ) -> dict:
        """
        Request kill switch activation/deactivation (REQUIRES HUMAN APPROVAL).

        URGENT use case — shorter TTL.

        Args:
            activate: True to activate (halt all trading), False to deactivate
            reason: Required reason (≤500 chars)
            requested_by: Caller identity
        """
        ctx = _make_tool_trace(config, "request_kill_switch")
        return _wrap_call(
            config, rate_limiter,
            "request_kill_switch",
            lambda **kw: wh.request_kill_switch(config, store, **kw),
            args={
                "activate": activate, "reason": reason,
                "requested_by": requested_by,
                "trace_id": ctx.trace_id,
            },
            audit_event_type="approval_request",
        )

    # ─── Tool 5: list_pending_approvals ───────
    @mcp.tool()
    def list_pending_approvals(limit: int = 20) -> dict:
        """
        List approvals currently pending decision.

        Args:
            limit: Max number to return (1-100)
        """
        return _wrap_call(
            config, rate_limiter,
            "list_pending_approvals",
            lambda **kw: wh.list_pending_approvals(config, store, **kw),
            args={"limit": limit},
        )

    # ─── Tool 6: get_approval_status ──────────
    @mcp.tool()
    def get_approval_status(approval_id: str) -> dict:
        """
        Get current status of a specific approval_id.

        Args:
            approval_id: e.g. "apv-20260507-a1b2c3d4"
        """
        return _wrap_call(
            config, rate_limiter,
            "get_approval_status",
            lambda **kw: wh.get_approval_status(config, store, **kw),
            args={"approval_id": approval_id},
        )

    # ─── Tool 7: cancel_request ───────────────
    @mcp.tool()
    def cancel_request(
        approval_id: str,
        reason: str = "",
        cancelled_by: str = "agent",
    ) -> dict:
        """
        Cancel a pending approval request (REQUESTER ONLY).

        Only the requester can cancel their own pending request.
        Cannot cancel approved/rejected/executed approvals.

        Args:
            approval_id: Approval to cancel
            reason: Cancellation reason
            cancelled_by: Must match the original requester
        """
        return _wrap_call(
            config, rate_limiter,
            "cancel_request",
            lambda **kw: wh.cancel_request(config, store, **kw),
            args={
                "approval_id": approval_id,
                "reason": reason,
                "cancelled_by": cancelled_by,
            },
        )

    # ─── Tool 8: execute_approved_action ──────
    @mcp.tool()
    def execute_approved_action(
        approval_id: str,
        executed_by: str = "agent",
    ) -> dict:
        """
        Execute a previously-approved action.

        Approval must be in 'approved' state.
        Single-use (idempotent) — execution marks status as 'executed'.

        Args:
            approval_id: Previously-approved approval_id
            executed_by: Caller identity
        """
        return _wrap_call(
            config, rate_limiter,
            "execute_approved_action",
            lambda **kw: wh.execute_approved_action(config, store, **kw),
            args={
                "approval_id": approval_id,
                "executed_by": executed_by,
            },
        )

    logger.info(
        f"jcpr-restricted MCP server built — 8 tools, "
        f"session={config.session_id}, "
        f"operator={config.operator_id}, "
        f"allow_live={config.allow_live}, "
        f"approval_db={config.approval_db}"
    )
    return mcp, store


# ─────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────

__all__ = [
    "build_server",
    "RestrictedServerConfig",
    "load_restricted_config_from_env",
]
