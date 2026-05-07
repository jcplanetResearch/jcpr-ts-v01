"""
Write 핸들러 (Write Handlers)
==============================

JCPR Trading System - jcpr-ts-v01
Task 35 v0.1

restricted MCP 서버의 핵심 로직 — request 4 + 관리 3 + execute 1.
(Core logic for restricted MCP server.)

원칙:
    - 모든 write는 ApprovalRecord 만들고 pending 상태로
    - 실제 실행은 별도 execute_approved_action 호출 시
    - paper_mode 검증은 요청 단계 + 실행 단계 모두
    - 도메인 실행은 stub (Task 21 ExecutionGateway 미통합)
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from ._approval_store import (
    ACTION_CANCEL_ORDER,
    ACTION_KILL_SWITCH,
    ACTION_SET_CAPACITY,
    ACTION_SUBMIT_ORDER,
    ApprovalNotFound,
    ApprovalRecord,
    ApprovalStateError,
    ApprovalStore,
    ApprovalStoreError,
    SelfApprovalError,
    STATUS_APPROVED,
    STATUS_PENDING,
)
from ._config import RestrictedServerConfig
from ._security import (
    mask_output,
    validate_iso_datetime,
    validate_limit,
    validate_symbol,
)


# ─────────────────────────────────────────────────
# 표준 응답 (Standard Response)
# ─────────────────────────────────────────────────

def _ok(**data) -> dict[str, Any]:
    return {
        "ok": True,
        "computed_at_utc": datetime.now(timezone.utc).isoformat(),
        **data,
    }


def _err(code: str, msg: str, **extra) -> dict[str, Any]:
    return {
        "ok": False,
        "error_code": code,
        "error_message": msg,
        **extra,
    }


def _record_to_dict(rec: ApprovalRecord) -> dict[str, Any]:
    """LLM-친화적 응답 dict."""
    d = rec.to_dict()
    # 만료까지 남은 시간 (선택 정보)
    now = datetime.now(timezone.utc)
    if rec.status == STATUS_PENDING:
        remaining = (rec.expires_at_utc - now).total_seconds()
        d["expires_in_seconds"] = max(0, int(remaining))
    return d


# ─────────────────────────────────────────────────
# 입력 검증 — Action별
# ─────────────────────────────────────────────────

def _validate_submit_order_payload(payload: dict) -> dict:
    """submit_order payload 검증 + 정규화."""
    errors: list[str] = []

    # symbol
    symbol = payload.get("symbol")
    try:
        symbol = validate_symbol(symbol)
        if symbol is None:
            errors.append("symbol required")
    except ValueError as e:
        errors.append(str(e))

    # side
    side = payload.get("side", "").lower() if payload.get("side") else ""
    if side not in ("buy", "sell"):
        errors.append(f"side must be 'buy' or 'sell', got {side!r}")

    # qty (정수 또는 string)
    qty_raw = payload.get("qty")
    qty_int = 0
    try:
        qty_int = int(str(qty_raw))
        if qty_int <= 0:
            errors.append(f"qty must be > 0, got {qty_int}")
        if qty_int > 1_000_000:
            errors.append(f"qty too large: {qty_int}")
    except (ValueError, TypeError):
        errors.append(f"qty must be positive integer, got {qty_raw!r}")

    # price_krw (선택 — limit order)
    price_str: Optional[str] = None
    price_raw = payload.get("price_krw")
    if price_raw is not None and price_raw != "":
        try:
            p = Decimal(str(price_raw))
            if p <= 0:
                errors.append(f"price_krw must be > 0, got {p}")
            if p > Decimal("100000000"):  # 1억원/주 한도
                errors.append(f"price_krw too large: {p}")
            price_str = str(p)
        except Exception:  # noqa: BLE001
            errors.append(f"price_krw must be valid decimal, got {price_raw!r}")

    # order_type
    order_type = payload.get("order_type", "market").lower()
    if order_type not in ("market", "limit"):
        errors.append(f"order_type must be 'market' or 'limit', got {order_type!r}")
    if order_type == "limit" and price_str is None:
        errors.append("limit order requires price_krw")

    # mode
    mode = payload.get("mode", "paper").lower()
    if mode not in ("paper", "live"):
        errors.append(f"mode must be 'paper' or 'live', got {mode!r}")

    # strategy_id (선택)
    strategy_id = payload.get("strategy_id")
    if strategy_id is not None:
        if not isinstance(strategy_id, str) or len(strategy_id) > 64:
            errors.append("strategy_id must be str ≤ 64 chars")
        elif not re.match(r"^[a-zA-Z0-9_\-]+$", strategy_id):
            errors.append("strategy_id invalid characters")

    # idempotency_token (선택 — 클라이언트가 재시도 시)
    idem_token = payload.get("client_order_id")
    if idem_token is not None:
        if not isinstance(idem_token, str) or len(idem_token) > 64:
            errors.append("client_order_id must be str ≤ 64 chars")

    if errors:
        raise ValueError("; ".join(errors))

    return {
        "symbol": symbol,
        "side": side,
        "qty": qty_int,
        "price_krw": price_str,
        "order_type": order_type,
        "mode": mode,
        "strategy_id": strategy_id,
        "client_order_id": idem_token,
    }


def _validate_cancel_order_payload(payload: dict) -> dict:
    """cancel_order payload 검증."""
    errors: list[str] = []

    order_id = payload.get("order_id")
    if not order_id or not isinstance(order_id, str):
        errors.append("order_id required")
    elif len(order_id) > 128:
        errors.append("order_id too long")
    elif not re.match(r"^[a-zA-Z0-9_\-]+$", order_id):
        errors.append("order_id invalid characters")

    reason = payload.get("reason", "")
    if not isinstance(reason, str):
        errors.append("reason must be str")
    elif len(reason) > 500:
        errors.append("reason too long (max 500 chars)")

    if errors:
        raise ValueError("; ".join(errors))

    return {"order_id": order_id, "reason": reason}


def _validate_set_capacity_payload(payload: dict) -> dict:
    """set_capacity payload 검증."""
    errors: list[str] = []

    capacity_raw = payload.get("capacity_krw")
    capacity_str: Optional[str] = None
    try:
        c = Decimal(str(capacity_raw))
        if c < 0:
            errors.append(f"capacity_krw must be ≥ 0, got {c}")
        if c > Decimal("100000000000"):  # 1000억 한도
            errors.append(f"capacity_krw too large: {c}")
        capacity_str = str(c)
    except Exception:  # noqa: BLE001
        errors.append(f"capacity_krw must be valid decimal")

    target = payload.get("target", "total")
    if target not in ("total", "per_strategy"):
        errors.append(f"target must be 'total' or 'per_strategy'")

    strategy_id: Optional[str] = None
    if target == "per_strategy":
        sid = payload.get("strategy_id")
        if not sid or not isinstance(sid, str):
            errors.append("strategy_id required for per_strategy target")
        elif not re.match(r"^[a-zA-Z0-9_\-]+$", sid):
            errors.append("strategy_id invalid characters")
        else:
            strategy_id = sid

    reason = payload.get("reason", "")
    if not isinstance(reason, str) or len(reason) > 500:
        errors.append("reason must be str ≤ 500 chars")

    if errors:
        raise ValueError("; ".join(errors))

    return {
        "capacity_krw": capacity_str,
        "target": target,
        "strategy_id": strategy_id,
        "reason": reason,
    }


def _validate_kill_switch_payload(payload: dict) -> dict:
    """kill_switch payload 검증."""
    errors: list[str] = []

    activate = payload.get("activate")
    if not isinstance(activate, bool):
        errors.append("activate must be bool")

    reason = payload.get("reason", "")
    if not isinstance(reason, str) or len(reason) > 500:
        errors.append("reason must be str ≤ 500 chars")
    if activate and not reason:
        errors.append("reason required when activating kill_switch")

    if errors:
        raise ValueError("; ".join(errors))
    return {"activate": activate, "reason": reason}


# ─────────────────────────────────────────────────
# Live 모드 검증 (Live Mode Gate)
# ─────────────────────────────────────────────────

def _enforce_paper_or_validate_live(
    config: RestrictedServerConfig,
    payload_mode: Optional[str],
) -> bool:
    """
    paper_only 강제 + live 허용 조건 검증.

    Returns:
        paper_mode (True if paper, False if live)

    Raises:
        ValueError: live 요청인데 허용 조건 미충족
    """
    requested_mode = (payload_mode or "paper").lower()
    if requested_mode == "paper":
        return True
    if requested_mode == "live":
        if not config.allow_live:
            raise ValueError(
                "Live 모드 비활성 — 설정 allow_live=True 필요. "
                "(JCPR_ALLOW_LIVE=1 환경변수 + config 변경)"
            )
        return False
    raise ValueError(f"unknown mode: {requested_mode!r}")


# ─────────────────────────────────────────────────
# Tool: request_submit_order
# ─────────────────────────────────────────────────

def request_submit_order(
    config: RestrictedServerConfig,
    store: ApprovalStore,
    *,
    symbol: str,
    side: str,
    qty: int,
    order_type: str = "market",
    price_krw: Optional[str] = None,
    mode: str = "paper",
    strategy_id: Optional[str] = None,
    client_order_id: Optional[str] = None,
    requested_by: str,
    trace_id: str,
    parent_trace_id: Optional[str] = None,
) -> dict[str, Any]:
    """주문 요청 — pending approval 등록."""
    try:
        validated = _validate_submit_order_payload({
            "symbol": symbol, "side": side, "qty": qty,
            "order_type": order_type, "price_krw": price_krw,
            "mode": mode, "strategy_id": strategy_id,
            "client_order_id": client_order_id,
        })
        # paper/live 검증
        paper_mode = _enforce_paper_or_validate_live(config, validated["mode"])

        rec = store.create_request(
            action_type=ACTION_SUBMIT_ORDER,
            requested_by=requested_by,
            payload=validated,
            trace_id=trace_id,
            parent_trace_id=parent_trace_id,
            paper_mode=paper_mode,
            custom_ttl_seconds=config.approval_ttl_seconds,
        )
        return mask_output(_ok(
            tool="request_submit_order",
            **_record_to_dict(rec),
        ))
    except ValueError as e:
        return _err("VALIDATION_ERROR", str(e))
    except ApprovalStoreError as e:
        return _err("STORE_ERROR", str(e))
    except Exception as e:  # noqa: BLE001
        return _err("REQUEST_ERROR", f"{type(e).__name__}: {e}")


# ─────────────────────────────────────────────────
# Tool: request_cancel_order
# ─────────────────────────────────────────────────

def request_cancel_order(
    config: RestrictedServerConfig,
    store: ApprovalStore,
    *,
    order_id: str,
    reason: str = "",
    requested_by: str,
    trace_id: str,
    parent_trace_id: Optional[str] = None,
) -> dict[str, Any]:
    """주문 취소 요청."""
    try:
        validated = _validate_cancel_order_payload({
            "order_id": order_id, "reason": reason,
        })
        rec = store.create_request(
            action_type=ACTION_CANCEL_ORDER,
            requested_by=requested_by,
            payload=validated,
            trace_id=trace_id,
            parent_trace_id=parent_trace_id,
            paper_mode=True,  # cancel은 mode 무관
            custom_ttl_seconds=config.approval_ttl_seconds,
        )
        return mask_output(_ok(
            tool="request_cancel_order",
            **_record_to_dict(rec),
        ))
    except ValueError as e:
        return _err("VALIDATION_ERROR", str(e))
    except ApprovalStoreError as e:
        return _err("STORE_ERROR", str(e))
    except Exception as e:  # noqa: BLE001
        return _err("REQUEST_ERROR", f"{type(e).__name__}: {e}")


# ─────────────────────────────────────────────────
# Tool: request_set_capacity
# ─────────────────────────────────────────────────

def request_set_capacity(
    config: RestrictedServerConfig,
    store: ApprovalStore,
    *,
    capacity_krw: str,
    target: str = "total",
    strategy_id: Optional[str] = None,
    reason: str = "",
    requested_by: str,
    trace_id: str,
    parent_trace_id: Optional[str] = None,
) -> dict[str, Any]:
    """자본 한도 변경 요청."""
    try:
        validated = _validate_set_capacity_payload({
            "capacity_krw": capacity_krw,
            "target": target,
            "strategy_id": strategy_id,
            "reason": reason,
        })
        rec = store.create_request(
            action_type=ACTION_SET_CAPACITY,
            requested_by=requested_by,
            payload=validated,
            trace_id=trace_id,
            parent_trace_id=parent_trace_id,
            paper_mode=True,
            custom_ttl_seconds=config.approval_ttl_seconds,
        )
        return mask_output(_ok(
            tool="request_set_capacity",
            **_record_to_dict(rec),
        ))
    except ValueError as e:
        return _err("VALIDATION_ERROR", str(e))
    except ApprovalStoreError as e:
        return _err("STORE_ERROR", str(e))
    except Exception as e:  # noqa: BLE001
        return _err("REQUEST_ERROR", f"{type(e).__name__}: {e}")


# ─────────────────────────────────────────────────
# Tool: request_kill_switch
# ─────────────────────────────────────────────────

def request_kill_switch(
    config: RestrictedServerConfig,
    store: ApprovalStore,
    *,
    activate: bool,
    reason: str,
    requested_by: str,
    trace_id: str,
    parent_trace_id: Optional[str] = None,
) -> dict[str, Any]:
    """긴급 정지 요청."""
    try:
        validated = _validate_kill_switch_payload({
            "activate": activate, "reason": reason,
        })
        rec = store.create_request(
            action_type=ACTION_KILL_SWITCH,
            requested_by=requested_by,
            payload=validated,
            trace_id=trace_id,
            parent_trace_id=parent_trace_id,
            paper_mode=True,
            custom_ttl_seconds=min(config.approval_ttl_seconds, 60),
            # kill_switch는 더 짧은 TTL (긴급)
        )
        return mask_output(_ok(
            tool="request_kill_switch",
            **_record_to_dict(rec),
        ))
    except ValueError as e:
        return _err("VALIDATION_ERROR", str(e))
    except ApprovalStoreError as e:
        return _err("STORE_ERROR", str(e))
    except Exception as e:  # noqa: BLE001
        return _err("REQUEST_ERROR", f"{type(e).__name__}: {e}")


# ─────────────────────────────────────────────────
# Tool: list_pending_approvals
# ─────────────────────────────────────────────────

def list_pending_approvals(
    config: RestrictedServerConfig,
    store: ApprovalStore,
    *,
    limit: Optional[int] = None,
) -> dict[str, Any]:
    """대기 중 승인 목록."""
    try:
        n = validate_limit(
            limit, default=20, max_value=config.max_pending_returned,
        )
        records = store.list_pending(limit=n)
        return mask_output(_ok(
            tool="list_pending_approvals",
            pending_approvals=[_record_to_dict(r) for r in records],
            count=len(records),
        ))
    except ValueError as e:
        return _err("VALIDATION_ERROR", str(e))
    except Exception as e:  # noqa: BLE001
        return _err("LIST_ERROR", f"{type(e).__name__}: {e}")


# ─────────────────────────────────────────────────
# Tool: get_approval_status
# ─────────────────────────────────────────────────

def get_approval_status(
    config: RestrictedServerConfig,
    store: ApprovalStore,
    *,
    approval_id: str,
) -> dict[str, Any]:
    """approval_id 상태 조회."""
    try:
        if not approval_id or not re.match(
            r"^apv-\d{8}-[a-f0-9]{8,16}$", approval_id
        ):
            return _err("VALIDATION_ERROR", "approval_id 형식 오류")
        rec = store.get_optional(approval_id)
        if rec is None:
            return _err("NOT_FOUND", f"approval_id {approval_id} 없음")
        return mask_output(_ok(
            tool="get_approval_status",
            **_record_to_dict(rec),
        ))
    except Exception as e:  # noqa: BLE001
        return _err("STATUS_ERROR", f"{type(e).__name__}: {e}")


# ─────────────────────────────────────────────────
# Tool: cancel_request (요청자 본인)
# ─────────────────────────────────────────────────

def cancel_request(
    config: RestrictedServerConfig,
    store: ApprovalStore,
    *,
    approval_id: str,
    reason: str = "",
    cancelled_by: str,
) -> dict[str, Any]:
    """요청자 본인이 미승인 요청 취소."""
    try:
        if not re.match(r"^apv-\d{8}-[a-f0-9]{8,16}$", approval_id):
            return _err("VALIDATION_ERROR", "approval_id 형식 오류")
        if not isinstance(reason, str) or len(reason) > 500:
            return _err("VALIDATION_ERROR", "reason must be str ≤ 500 chars")
        rec = store.cancel(
            approval_id, cancelled_by=cancelled_by, reason=reason,
        )
        return mask_output(_ok(
            tool="cancel_request",
            **_record_to_dict(rec),
        ))
    except ApprovalNotFound:
        return _err("NOT_FOUND", f"approval_id {approval_id} 없음")
    except ApprovalStateError as e:
        return _err("STATE_ERROR", str(e))
    except ApprovalStoreError as e:
        return _err("STORE_ERROR", str(e))
    except Exception as e:  # noqa: BLE001
        return _err("CANCEL_ERROR", f"{type(e).__name__}: {e}")


# ─────────────────────────────────────────────────
# Tool: execute_approved_action (실행)
# ─────────────────────────────────────────────────

def execute_approved_action(
    config: RestrictedServerConfig,
    store: ApprovalStore,
    *,
    approval_id: str,
    executed_by: str,
) -> dict[str, Any]:
    """
    승인된 action 실행.

    실제 도메인 실행은 Task 21 ExecutionGateway 미통합이므로
    stub 결과 반환. Task 40에서 실제 broker 호출로 교체.
    """
    try:
        if not re.match(r"^apv-\d{8}-[a-f0-9]{8,16}$", approval_id):
            return _err("VALIDATION_ERROR", "approval_id 형식 오류")

        # 1. 상태 확인
        rec = store.get_optional(approval_id)
        if rec is None:
            return _err("NOT_FOUND", f"approval_id {approval_id} 없음")
        if rec.status != STATUS_APPROVED:
            return _err(
                "STATE_ERROR",
                f"approval status={rec.status} — only 'approved' can execute",
            )

        # 2. 도메인 실행 (현재는 stub)
        execution_result = _execute_action_stub(rec, config)

        # 3. 상태 업데이트
        try:
            updated = store.mark_executed(
                approval_id,
                execution_result=execution_result,
                executed_by=executed_by,
            )
        except ApprovalStateError as e:
            # execute_ttl 만료 등
            return _err("STATE_ERROR", str(e))

        return mask_output(_ok(
            tool="execute_approved_action",
            **_record_to_dict(updated),
        ))
    except ApprovalStoreError as e:
        return _err("STORE_ERROR", str(e))
    except Exception as e:  # noqa: BLE001
        return _err("EXECUTE_ERROR", f"{type(e).__name__}: {e}")


def _execute_action_stub(
    rec: ApprovalRecord,
    config: RestrictedServerConfig,
) -> dict[str, Any]:
    """도메인 실행 stub — Task 21 미통합."""
    note = (
        "STUB EXECUTION — Task 21 ExecutionGateway 미통합. "
        "실제 broker 호출은 다음 세션 Task 40에서."
    )
    return {
        "executed": True,
        "stub": True,
        "action_type": rec.action_type,
        "paper_mode": rec.paper_mode,
        "note": note,
        "summary": _summarize_payload_for_audit(rec),
    }


def _summarize_payload_for_audit(rec: ApprovalRecord) -> dict:
    """audit/응답용 payload 요약 (시크릿 자동 마스킹)."""
    if rec.action_type == ACTION_SUBMIT_ORDER:
        p = rec.payload
        return {
            "symbol": p.get("symbol"),
            "side": p.get("side"),
            "qty": p.get("qty"),
            "order_type": p.get("order_type"),
        }
    if rec.action_type == ACTION_CANCEL_ORDER:
        return {"order_id": rec.payload.get("order_id")}
    if rec.action_type == ACTION_SET_CAPACITY:
        return {
            "target": rec.payload.get("target"),
            "capacity_krw": rec.payload.get("capacity_krw"),
        }
    if rec.action_type == ACTION_KILL_SWITCH:
        return {"activate": rec.payload.get("activate")}
    return {}


# ─────────────────────────────────────────────────
# Internal: approve_action (운영자 전용 — CLI에서)
# ─────────────────────────────────────────────────

def approve_action(
    config: RestrictedServerConfig,
    store: ApprovalStore,
    *,
    approval_id: str,
    decided_by: str,
    reason: str = "",
) -> dict[str, Any]:
    """
    [INTERNAL] 운영자 승인 — CLI/대시보드에서 호출.

    Agent의 MCP 도구로는 노출되지 않음.
    """
    try:
        if not re.match(r"^apv-\d{8}-[a-f0-9]{8,16}$", approval_id):
            return _err("VALIDATION_ERROR", "approval_id 형식 오류")
        if not decided_by or not isinstance(decided_by, str):
            return _err("VALIDATION_ERROR", "decided_by required")

        rec = store.approve(
            approval_id, decided_by=decided_by, reason=reason,
        )
        return mask_output(_ok(
            tool="approve_action",
            **_record_to_dict(rec),
        ))
    except ApprovalNotFound:
        return _err("NOT_FOUND", f"approval_id {approval_id} 없음")
    except SelfApprovalError as e:
        return _err("SELF_APPROVAL_BLOCKED", str(e))
    except ApprovalStateError as e:
        return _err("STATE_ERROR", str(e))
    except ApprovalStoreError as e:
        return _err("STORE_ERROR", str(e))
    except Exception as e:  # noqa: BLE001
        return _err("APPROVE_ERROR", f"{type(e).__name__}: {e}")


# ─────────────────────────────────────────────────
# Internal: reject_action (운영자 전용)
# ─────────────────────────────────────────────────

def reject_action(
    config: RestrictedServerConfig,
    store: ApprovalStore,
    *,
    approval_id: str,
    decided_by: str,
    reason: str = "",
) -> dict[str, Any]:
    """[INTERNAL] 운영자 거부."""
    try:
        if not re.match(r"^apv-\d{8}-[a-f0-9]{8,16}$", approval_id):
            return _err("VALIDATION_ERROR", "approval_id 형식 오류")
        if not decided_by or not isinstance(decided_by, str):
            return _err("VALIDATION_ERROR", "decided_by required")

        rec = store.reject(
            approval_id, decided_by=decided_by, reason=reason,
        )
        return mask_output(_ok(
            tool="reject_action",
            **_record_to_dict(rec),
        ))
    except ApprovalNotFound:
        return _err("NOT_FOUND", f"approval_id {approval_id} 없음")
    except SelfApprovalError as e:
        return _err("SELF_APPROVAL_BLOCKED", str(e))
    except ApprovalStateError as e:
        return _err("STATE_ERROR", str(e))
    except ApprovalStoreError as e:
        return _err("STORE_ERROR", str(e))
    except Exception as e:  # noqa: BLE001
        return _err("REJECT_ERROR", f"{type(e).__name__}: {e}")


# ─────────────────────────────────────────────────
# 도구 목록 (Tool Inventory)
# ─────────────────────────────────────────────────

# MCP 노출 (8개)
EXPOSED_TOOLS = (
    "request_submit_order",
    "request_cancel_order",
    "request_set_capacity",
    "request_kill_switch",
    "list_pending_approvals",
    "get_approval_status",
    "cancel_request",
    "execute_approved_action",
)

# 내부 (CLI에서만, 2개)
INTERNAL_TOOLS = (
    "approve_action",
    "reject_action",
)
