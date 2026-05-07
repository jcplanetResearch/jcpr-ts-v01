"""
MCP Read-Only 서버 (MCP Read-Only Server)
============================================

JCPR Trading System - jcpr-ts-v01
Task 34 v0.1

LLM Agent에게 read-only 데이터 접근만 제공하는 MCP 서버.
(MCP server providing read-only data access to LLM agents.)

설계 (Design):
    - FastMCP 3.0 decorator 기반
    - stdio 전용 (외부 노출 차단)
    - 자격증명 처리 절대 금지
    - 모든 호출 자동 audit (Task A1-A3)
    - Rate limit + 입력 검증 + 출력 마스킹

사용 (Usage):
    # 서버 생성
    server = build_server()
    
    # stdio 모드 실행 (external entrypoint)
    asyncio.run(server.run_stdio_async())

이 파일은 MCP server framework. 실제 실행은
scripts/run_readonly_mcp.py 가 담당.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from src.observability import (
    AuditWriter,
    ORIGIN_AGENT,
    TraceContext,
    configure_default_writer,
    get_default_writer,
)

from ._config import ReadOnlyServerConfig, load_config_from_env
from ._security import (
    RateLimiter,
    check_result_size,
    mask_output,
)
from . import _tool_handlers as handlers


# ─────────────────────────────────────────────────
# 로깅 (Logging) — STDIO 안전
# ─────────────────────────────────────────────────
# stdio 서버는 stdout 사용 금지 (JSON-RPC 손상)
# stderr만 사용

logger = logging.getLogger("jcpr.mcp.readonly")
_handler_set = False


def _setup_logging() -> None:
    """stderr 로깅 설정 — 한 번만."""
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
# Trace 헬퍼
# ─────────────────────────────────────────────────

def _make_tool_trace(
    config: ReadOnlyServerConfig,
    tool_name: str,
    correlation: Optional[dict] = None,
) -> TraceContext:
    """
    각 tool 호출용 TraceContext 생성.

    Origin: agent (MCP server는 LLM agent의 호출만 받음).
    """
    keys: dict[str, Any] = {"tool": tool_name, "server": "readonly_mcp"}
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
    """tool 호출 시작 audit."""
    if writer is None:
        return
    writer.write_mcp_call(ctx, payload={
        "tool": tool_name,
        "args": args,  # 자동 마스킹됨 (Task A2)
    })


def _audit_call_result(
    writer: Optional[AuditWriter],
    ctx: TraceContext,
    tool_name: str,
    result: dict,
) -> None:
    """tool 결과 audit — 큰 데이터는 요약만."""
    if writer is None:
        return
    summary = {
        "tool": tool_name,
        "ok": result.get("ok"),
        "result_keys": list(result.keys())[:20],
    }
    if not result.get("ok"):
        summary["error_code"] = result.get("error_code")
        summary["error_message"] = result.get("error_message", "")[:200]
    writer.write_mcp_result(ctx, payload=summary)


# ─────────────────────────────────────────────────
# Rate-limit + audit wrapper
# ─────────────────────────────────────────────────

def _wrap_call(
    config: ReadOnlyServerConfig,
    rate_limiter: RateLimiter,
    tool_name: str,
    handler_fn,
    args: dict,
) -> dict:
    """
    공통 wrapper: rate limit → audit start → handler → audit result → 크기 검증.

    Args:
        config: 서버 설정
        rate_limiter: 공유 RateLimiter
        tool_name: 도구 이름
        handler_fn: callable(config, **args) → dict
        args: 도구 인자 dict

    Returns:
        결과 dict (또는 에러 응답)
    """
    writer = get_default_writer()
    ctx = _make_tool_trace(config, tool_name, correlation={
        "args_keys": list(args.keys()),
    })

    # ─── Rate limit ───────────────────────────
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

    # ─── Handler 호출 ─────────────────────────
    try:
        result = handler_fn(config, **args)
    except TypeError as e:
        result = {
            "ok": False,
            "error_code": "INVALID_ARGS",
            "error_message": str(e),
        }
    except Exception as e:  # noqa: BLE001
        if writer is not None:
            writer.write_exception(ctx, e, additional={"tool": tool_name})
        result = {
            "ok": False,
            "error_code": "HANDLER_ERROR",
            "error_message": f"{type(e).__name__}: {e}",
        }

    # ─── 크기 검증 ────────────────────────────
    try:
        serialized = json.dumps(result, ensure_ascii=False, default=str)
        ok_size, msg = check_result_size(serialized)
        if not ok_size:
            result = {
                "ok": False,
                "error_code": "RESULT_TOO_LARGE",
                "error_message": msg,
            }
    except Exception as e:  # noqa: BLE001
        result = {
            "ok": False,
            "error_code": "SERIALIZATION_ERROR",
            "error_message": str(e),
        }

    # ─── Audit ────────────────────────────────
    _audit_call_result(writer, ctx, tool_name, result)
    # trace_id는 결과에 첨부 (LLM이 사후 추적할 수 있도록)
    result["_trace_id"] = ctx.trace_id
    return result


# ─────────────────────────────────────────────────
# 서버 빌더 (Server Builder)
# ─────────────────────────────────────────────────

def build_server(
    config: Optional[ReadOnlyServerConfig] = None,
) -> FastMCP:
    """
    FastMCP 서버 인스턴스 생성 + 8개 도구 등록.

    Args:
        config: 서버 설정 (None이면 환경변수에서 로드)

    Returns:
        FastMCP 인스턴스 — 호출자가 .run_stdio_async() 실행
    """
    _setup_logging()

    if config is None:
        config = load_config_from_env()

    # AuditWriter 설정 (한 번만)
    if get_default_writer() is None:
        configure_default_writer(config.audit_dir)
        logger.info(f"AuditWriter configured: {config.audit_dir}")

    rate_limiter = RateLimiter(max_per_minute=config.rate_limit_per_minute)

    # FastMCP 인스턴스
    mcp = FastMCP(
        name="jcpr-readonly",
        instructions=(
            "JCPR Trading System read-only MCP server. "
            "All tool calls are audited with trace IDs. "
            "No write operations exposed. "
            "All credentials are forbidden in this server."
        ),
    )

    # ─────────────────────────────────────────
    # Tool 1: get_market_status
    # ─────────────────────────────────────────
    @mcp.tool()
    def get_market_status() -> dict:
        """
        Get current KRX market status.

        Returns market state (open/closed/pre_market), KST time,
        trading day flag, and approximate session info.

        No inputs required. Read-only.
        """
        return _wrap_call(
            config, rate_limiter,
            "get_market_status",
            handlers.get_market_status,
            args={},
        )

    # ─────────────────────────────────────────
    # Tool 2: get_positions
    # ─────────────────────────────────────────
    @mcp.tool()
    def get_positions() -> dict:
        """
        Get current open positions from the trading system.

        Returns list of positions with symbol, qty, market_value_krw, etc.
        Read-only access to positions database.
        """
        return _wrap_call(
            config, rate_limiter,
            "get_positions",
            handlers.get_positions,
            args={},
        )

    # ─────────────────────────────────────────
    # Tool 3: get_pnl_snapshot
    # ─────────────────────────────────────────
    @mcp.tool()
    def get_pnl_snapshot(
        starting_capital_krw: str,
        cash_krw: str,
    ) -> dict:
        """
        Compute P&L snapshot.

        Args:
            starting_capital_krw: Initial capital as decimal string (e.g. "10000000")
            cash_krw: Current cash as decimal string

        Returns equity, pnl, pnl_pct based on current positions + cash.
        """
        return _wrap_call(
            config, rate_limiter,
            "get_pnl_snapshot",
            handlers.get_pnl_snapshot,
            args={
                "starting_capital_krw": starting_capital_krw,
                "cash_krw": cash_krw,
            },
        )

    # ─────────────────────────────────────────
    # Tool 4: get_recent_fills
    # ─────────────────────────────────────────
    @mcp.tool()
    def get_recent_fills(
        limit: int = 50,
        since_iso: Optional[str] = None,
    ) -> dict:
        """
        Get recent fills.

        Args:
            limit: Max number of fills to return (1-500)
            since_iso: ISO 8601 datetime to filter fills (UTC)

        Returns list of fills sorted by timestamp descending.
        """
        return _wrap_call(
            config, rate_limiter,
            "get_recent_fills",
            handlers.get_recent_fills,
            args={"limit": limit, "since_iso": since_iso},
        )

    # ─────────────────────────────────────────
    # Tool 5: get_rejection_summary
    # ─────────────────────────────────────────
    @mcp.tool()
    def get_rejection_summary(
        since_iso: Optional[str] = None,
    ) -> dict:
        """
        Summarize risk gate rejections.

        Args:
            since_iso: ISO 8601 datetime cutoff (UTC)

        Returns counts by_reason, by_gate, total_decisions.
        """
        return _wrap_call(
            config, rate_limiter,
            "get_rejection_summary",
            handlers.get_rejection_summary,
            args={"since_iso": since_iso},
        )

    # ─────────────────────────────────────────
    # Tool 6: get_portfolio_risk
    # ─────────────────────────────────────────
    @mcp.tool()
    def get_portfolio_risk(
        sector_map: Optional[dict] = None,
        cash_krw: str = "0",
    ) -> dict:
        """
        Analyze portfolio risk with sector concentration.

        Args:
            sector_map: {symbol: sector} dict for sector classification
            cash_krw: Current cash as decimal string

        Returns full risk snapshot with warnings, severity, HHI, by_sector.
        """
        return _wrap_call(
            config, rate_limiter,
            "get_portfolio_risk",
            handlers.get_portfolio_risk,
            args={"sector_map": sector_map, "cash_krw": cash_krw},
        )

    # ─────────────────────────────────────────
    # Tool 7: get_strategy_registry
    # ─────────────────────────────────────────
    @mcp.tool()
    def get_strategy_registry() -> dict:
        """
        Get strategy registry summary.

        Returns active/paper/live counts, capital weights,
        and per-strategy metadata (no parameters/secrets).
        """
        return _wrap_call(
            config, rate_limiter,
            "get_strategy_registry",
            handlers.get_strategy_registry,
            args={},
        )

    # ─────────────────────────────────────────
    # Tool 8: get_trace
    # ─────────────────────────────────────────
    @mcp.tool()
    def get_trace(
        trace_id: str,
        include_tree: bool = True,
    ) -> dict:
        """
        Get all events for a trace_id with optional span tree.

        Args:
            trace_id: e.g. "trc-20260507-a1b2c3d4"
            include_tree: Include span hierarchy tree

        Returns events (sorted), summary, and optional tree.
        """
        return _wrap_call(
            config, rate_limiter,
            "get_trace",
            handlers.get_trace,
            args={"trace_id": trace_id, "include_tree": include_tree},
        )

    logger.info(
        f"jcpr-readonly MCP server built — 8 tools registered, "
        f"session={config.session_id}, "
        f"rate_limit={config.rate_limit_per_minute}/min"
    )
    return mcp


# ─────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────

__all__ = [
    "build_server",
    "ReadOnlyServerConfig",
    "load_config_from_env",
]
