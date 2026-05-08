"""Task 40 — ExecutionGateway.

The ONLY component allowed to invoke broker write operations.

Workflow:
    1. Agent (Task 38/39) calls gateway.propose_order(order_request)
       → returns approval_id, state=PROPOSED
    2. Operator runs scripts/approve_cli.py → state=APPROVED
    3. Caller invokes gateway.execute_approved(approval_id)
       → state=EXECUTED, broker order placed via BrokerExecutionInterface

Defense in depth:
    1. Every broker call requires a valid APPROVED approval_id
    2. Self-approval blocked at ApprovalStore level
    3. Idempotency: re-executing an EXECUTED approval returns cached result
    4. ESC/Ctrl-C signal aborts in-flight calls
    5. All transitions logged via AuditWriter
    6. Expired approvals auto-rejected
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

from ..brokers.base import (
    BrokerExecutionInterface,
    BrokerMode,
    OrderRequest,
    OrderResponse,
    OrderSide,
    OrderStatus,
    OrderType,
)
from ._approval_state import (
    ApprovalError,
    ApprovalProposal,
    ApprovalState,
    ApprovalStore,
    ProposalNotFoundError,
    SelfApprovalError,
    StateTransitionError,
)


# Action type strings — must match what approve_cli.py expects
ACTION_PLACE_ORDER: str = "place_order"
ACTION_CANCEL_ORDER: str = "cancel_order"
ACTION_KILL_SWITCH: str = "kill_switch_engage"


class GatewayError(RuntimeError):
    """Gateway-specific errors."""


# =============================================================================
# Result types
# =============================================================================

@dataclass(frozen=True, slots=True)
class ProposalResult:
    """Result returned from propose_*."""
    approval_id: str
    state: ApprovalState
    expires_at_utc: datetime


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Result returned from execute_approved."""
    approval_id: str
    success: bool
    broker_order_id: str | None
    broker_response: OrderResponse | None
    error_message: str | None
    executed_at_utc: datetime


# =============================================================================
# Gateway
# =============================================================================

class ExecutionGateway:
    """Mediates between agents/operators and the broker write interface.

    Args:
        broker: BrokerExecutionInterface implementation (typically a KIS
                adapter that also implements this interface in Phase 2).
        store: ApprovalStore for state persistence.
        agent_actor: Default `requested_by` for proposals from this gateway.
                     Operators decide with a different actor id.
        audit_writer: Optional AuditWriter (Tasks A1-A3).

    Note: ExecutionGateway does NOT decide approvals — only proposes and
    executes. The CLI (approve_cli.py) is the only path to APPROVED.
    """

    def __init__(
        self,
        *,
        broker: BrokerExecutionInterface,
        store: ApprovalStore,
        agent_actor: str = "agent",
        audit_writer: Any | None = None,
        _now_fn: Any = None,
    ) -> None:
        if broker is None:
            raise ValueError("broker required")
        if store is None:
            raise ValueError("store required")
        if not agent_actor:
            raise ValueError("agent_actor required")
        self._broker = broker
        self._store = store
        self._agent_actor = agent_actor
        self._audit = audit_writer
        self._now_fn = _now_fn or (lambda: datetime.now(tz=timezone.utc))
        self._exec_lock = threading.Lock()
        self._interrupted = threading.Event()

    def signal_interrupt(self) -> None:
        """Tasks 29/30 signal handlers call this."""
        self._interrupted.set()

    # -------------------------------------------------------------------------
    # Propose
    # -------------------------------------------------------------------------

    def propose_order(
        self,
        *,
        order_request: OrderRequest,
        requested_by: str | None = None,
        expiry_sec: int = 300,
    ) -> ProposalResult:
        """Create a PROPOSED approval for a place_order action.

        Note: order_request.approval_id is OVERWRITTEN with the new approval id.
        Agents pass a placeholder; the gateway is the source of truth.
        """
        if order_request is None:
            raise ValueError("order_request required")
        if self._interrupted.is_set():
            raise GatewayError("interrupted — propose blocked")

        actor = requested_by or self._agent_actor

        payload = {
            "symbol": order_request.symbol,
            "side": order_request.side.value,
            "order_type": order_request.order_type.value,
            "quantity": str(order_request.quantity),
            "limit_price_krw": (str(order_request.limit_price_krw)
                                if order_request.limit_price_krw else None),
            "client_order_id": order_request.client_order_id,
            "strategy_id": order_request.strategy_id,
        }

        proposal = self._store.propose(
            action_type=ACTION_PLACE_ORDER,
            payload=payload,
            requested_by=actor,
            expiry_sec=expiry_sec,
        )

        self._audit_emit("proposal_created", {
            "approval_id": proposal.approval_id,
            "action_type": ACTION_PLACE_ORDER,
            "requested_by": actor,
            "symbol": order_request.symbol,
            "side": order_request.side.value,
            "quantity": str(order_request.quantity),
        })

        return ProposalResult(
            approval_id=proposal.approval_id,
            state=proposal.state,
            expires_at_utc=proposal.expires_at_utc,
        )

    def propose_cancel(
        self,
        *,
        broker_order_id: str,
        requested_by: str | None = None,
        expiry_sec: int = 300,
    ) -> ProposalResult:
        """Create a PROPOSED approval for cancel_order."""
        if not broker_order_id:
            raise ValueError("broker_order_id required")
        if self._interrupted.is_set():
            raise GatewayError("interrupted — propose blocked")

        actor = requested_by or self._agent_actor
        proposal = self._store.propose(
            action_type=ACTION_CANCEL_ORDER,
            payload={"broker_order_id": broker_order_id},
            requested_by=actor,
            expiry_sec=expiry_sec,
        )
        self._audit_emit("proposal_created", {
            "approval_id": proposal.approval_id,
            "action_type": ACTION_CANCEL_ORDER,
            "requested_by": actor,
        })
        return ProposalResult(
            approval_id=proposal.approval_id,
            state=proposal.state,
            expires_at_utc=proposal.expires_at_utc,
        )

    # -------------------------------------------------------------------------
    # Execute
    # -------------------------------------------------------------------------

    def execute_approved(
        self,
        *,
        approval_id: str,
    ) -> ExecutionResult:
        """Execute an APPROVED proposal. Idempotent on EXECUTED."""
        if not approval_id:
            raise ValueError("approval_id required")
        if self._interrupted.is_set():
            raise GatewayError("interrupted — execution blocked")

        with self._exec_lock:
            proposal = self._store.get(approval_id)

            # Idempotency: already executed → return cached result
            if proposal.state == ApprovalState.EXECUTED:
                cached = proposal.execution_result or {}
                return ExecutionResult(
                    approval_id=approval_id,
                    success=bool(cached.get("success", False)),
                    broker_order_id=cached.get("broker_order_id"),
                    broker_response=None,
                    error_message=cached.get("error_message"),
                    executed_at_utc=proposal.decided_at_utc or self._now_fn(),
                )

            if proposal.state != ApprovalState.APPROVED:
                raise GatewayError(
                    f"approval {approval_id} is in state "
                    f"{proposal.state.value}, must be APPROVED to execute"
                )

            # Dispatch on action_type
            try:
                if proposal.action_type == ACTION_PLACE_ORDER:
                    response = self._execute_place_order(proposal)
                elif proposal.action_type == ACTION_CANCEL_ORDER:
                    response = self._execute_cancel_order(proposal)
                else:
                    raise GatewayError(
                        f"unsupported action_type: {proposal.action_type}"
                    )
            except Exception as e:  # noqa: BLE001
                exec_result = {
                    "success": False,
                    "broker_order_id": None,
                    "error_message": str(e)[:500],
                }
                self._store.transition(
                    approval_id=approval_id,
                    target_state=ApprovalState.EXECUTED,
                    execution_result=exec_result,
                )
                self._audit_emit("execution_failed", {
                    "approval_id": approval_id,
                    "error": str(e)[:200],
                })
                return ExecutionResult(
                    approval_id=approval_id,
                    success=False,
                    broker_order_id=None,
                    broker_response=None,
                    error_message=str(e)[:500],
                    executed_at_utc=self._now_fn(),
                )

            exec_result = {
                "success": response.success,
                "broker_order_id": response.broker_order_id,
                "client_order_id": response.client_order_id,
                "status": response.status.value,
                "error_message": response.error_message,
            }
            self._store.transition(
                approval_id=approval_id,
                target_state=ApprovalState.EXECUTED,
                execution_result=exec_result,
            )
            self._audit_emit("execution_completed", {
                "approval_id": approval_id,
                "broker_order_id": response.broker_order_id,
                "success": response.success,
            })

            return ExecutionResult(
                approval_id=approval_id,
                success=response.success,
                broker_order_id=response.broker_order_id,
                broker_response=response,
                error_message=response.error_message,
                executed_at_utc=self._now_fn(),
            )

    def _execute_place_order(
        self, proposal: ApprovalProposal,
    ) -> OrderResponse:
        """Reconstruct OrderRequest from payload and call broker."""
        p = proposal.payload
        limit = p.get("limit_price_krw")
        request = OrderRequest(
            symbol=str(p["symbol"]),
            side=OrderSide(p["side"]),
            order_type=OrderType(p["order_type"]),
            quantity=Decimal(str(p["quantity"])),
            limit_price_krw=Decimal(str(limit)) if limit else None,
            client_order_id=str(p["client_order_id"]),
            strategy_id=str(p.get("strategy_id", "unknown")),
            approval_id=proposal.approval_id,
            requested_at_utc=proposal.proposed_at_utc,
        )
        return self._broker.place_order(request)

    def _execute_cancel_order(
        self, proposal: ApprovalProposal,
    ) -> OrderResponse:
        broker_order_id = str(proposal.payload["broker_order_id"])
        return self._broker.cancel_order(
            broker_order_id=broker_order_id,
            approval_id=proposal.approval_id,
        )

    # -------------------------------------------------------------------------
    # Audit
    # -------------------------------------------------------------------------

    def _audit_emit(self, event_type: str, payload: Mapping[str, Any]) -> None:
        if self._audit is None:
            return
        try:
            method = getattr(self._audit, "write_event", None)
            if callable(method):
                method(event_type=event_type, payload=dict(payload))
        except Exception:  # noqa: BLE001
            pass


__all__ = (
    "ExecutionGateway",
    "GatewayError",
    "ProposalResult",
    "ExecutionResult",
    "ACTION_PLACE_ORDER",
    "ACTION_CANCEL_ORDER",
    "ACTION_KILL_SWITCH",
)
