"""MCP write handlers — Phase 2, Phase 1 real ApprovalStore API 기준."""
from __future__ import annotations
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from src.execution._action_kind import ActionKind
from src.execution.approval_store import ApprovalState, ApprovalStore, ApprovalStoreError
try:
    from src.execution.approval_store import ApprovalExpiredError as ExpiredApprovalError
    from src.execution.approval_store import ApprovalStateError as InvalidStateTransitionError
    from src.execution.approval_store import SelfApprovalError
    from src.execution.approval_store import LiveModeBlockedError
except ImportError:
    ExpiredApprovalError = ApprovalStoreError       # type: ignore
    InvalidStateTransitionError = ApprovalStoreError  # type: ignore
    SelfApprovalError = ApprovalStoreError           # type: ignore
    LiveModeBlockedError = ApprovalStoreError        # type: ignore

from src.execution.execution_gateway import (
    ExecutionGateway, ExecutionResult, GatewayError, InterruptedExecutionError,
)

__all__ = ["WriteHandlerError", "WriteHandlers", "build_handlers"]

logger = logging.getLogger(__name__)


class WriteHandlerError(Exception):
    pass


def _record_payload(record) -> dict:
    v = getattr(record, "payload", None) or getattr(record, "action_payload", None)
    return dict(v) if v else {}


def _record_result(record) -> Optional[dict]:
    for attr in ("execution_result", "result", "execution_payload"):
        v = getattr(record, attr, None)
        if v is not None:
            return dict(v)
    return None


@dataclass(slots=True)
class WriteHandlers:
    """MCP write handler bundle.

    Phase 2-B signature change:
      Either `gateway` OR `mode` must be supplied (or both, if consistent).

      - gateway-only:   legacy mode — `mode` is read from gateway.mode.
                        All handlers (propose/cancel/list/execute) work.
      - mode-only:      lightweight/test mode — handlers can propose,
                        cancel, list, query, but execute_approved_action
                        is disabled (no gateway to dispatch to). Calling
                        execute raises WriteHandlerError, fail-closed.
      - both supplied:  requires mode == gateway.mode (rejected otherwise);
                        ensures handler-level mode tagging matches the
                        gateway routing decision (mode-routing consistency).

    Mode tagging is the source of the `mode` field in every approval
    record this handler creates; getting it wrong silently would
    mis-route execution. The post-init guard prevents that.
    """
    store: ApprovalStore
    gateway: Optional[ExecutionGateway] = None
    operator_id: str = "operator-jcpr"
    mode: Optional[str] = None
    proposal_ttl: int = 300
    execution_ttl: int = 60
    kill_switch_ttl: int = 60

    def __post_init__(self) -> None:
        # Fail-closed: never silently default mode to "paper" — operator
        # must declare intent via either gateway or explicit mode.
        if self.gateway is None and self.mode is None:
            raise ValueError(
                "WriteHandlers requires either gateway= or mode= "
                "(neither was provided)"
            )
        # Validate explicit mode value (gateway.mode is validated by
        # ExecutionGateway itself).
        if self.mode is not None and self.mode not in ("paper", "live"):
            raise ValueError(
                f"mode must be 'paper' or 'live', got {self.mode!r}"
            )
        # If both supplied, they must agree — mode-routing consistency.
        # Mismatch would let a 'live' propose tag through a 'paper'
        # gateway (or vice versa), creating an unreachable record.
        if self.gateway is not None and self.mode is not None:
            if self.mode != self.gateway.mode:
                raise ValueError(
                    f"WriteHandlers mode={self.mode!r} != "
                    f"gateway.mode={self.gateway.mode!r}; "
                    f"mode-routing must be consistent"
                )
        if not self.operator_id:
            raise ValueError("operator_id must be a non-empty string")

    # ------------------------------------------------------------------
    # Internal helpers — single source for mode + gateway access
    # ------------------------------------------------------------------

    def _effective_mode(self) -> str:
        """Resolve mode for record tagging.

        Prefers the gateway's mode when present (because that's the
        slot routing will use); falls back to the explicit `mode` field
        in mode-only configurations. The post-init guard guarantees at
        least one source is non-None, so this never returns None.
        """
        if self.gateway is not None:
            return self.gateway.mode
        return self.mode  # type: ignore[return-value]

    def _require_gateway(self, op: str) -> ExecutionGateway:
        """Demand a gateway for execute-side operations.

        Fail-closed: a handler bundle without a gateway cannot dispatch
        orders to a broker, so execute attempts on such bundles must be
        rejected explicitly rather than silently no-oping or raising
        AttributeError. Callers: execute_approved_action and any code
        that touches broker state directly.
        """
        if self.gateway is None:
            raise WriteHandlerError(
                f"{op} requires a configured ExecutionGateway "
                f"(this WriteHandlers was constructed mode-only; "
                f"execute is unavailable)"
            )
        return self.gateway

    # -- request_* -----------------------------------------------------------

    def request_submit_order(self, *, symbol, side, quantity, order_type,
                              requested_by, limit_price=None, time_in_force="DAY",
                              client_order_id=None, strategy_id=None) -> dict:
        self._require_actor(requested_by, role="requester")
        self._validate_self_distinct(requested_by)
        try:
            qty = Decimal(quantity)
        except Exception as exc:
            raise WriteHandlerError(f"invalid quantity {quantity!r}: {exc}") from exc
        lp = None
        if limit_price is not None:
            try:
                lp = Decimal(limit_price)
            except Exception as exc:
                raise WriteHandlerError(f"invalid limit_price {limit_price!r}: {exc}") from exc
        if order_type.upper() == "LIMIT" and lp is None:
            raise WriteHandlerError("LIMIT order requires limit_price")
        if side.upper() not in ("BUY", "SELL"):
            raise WriteHandlerError(f"side must be BUY or SELL, got {side!r}")
        if qty <= 0:
            raise WriteHandlerError("quantity must be > 0")

        # Phase 1: create_request(action_kind=str, payload=, requested_by=, mode=)
        record = self.store.create_request(
            action_kind=ActionKind.SUBMIT_ORDER.value,
            payload={
                "symbol": symbol,
                "side": side.upper(),
                "quantity": str(qty),
                "order_type": order_type.upper(),
                "limit_price_krw": str(lp) if lp is not None else None,
                "time_in_force": time_in_force.upper(),
                "client_order_id": client_order_id or self._generate_coid(),
                "strategy_id": strategy_id,
            },
            requested_by=requested_by,
            mode=self._effective_mode(),
        )
        return self._approval_summary_from_record(record)

    def request_cancel_order(self, *, broker_order_id, symbol, requested_by) -> dict:
        self._require_actor(requested_by, role="requester")
        self._validate_self_distinct(requested_by)
        if not broker_order_id:
            raise WriteHandlerError("broker_order_id is required")
        record = self.store.create_request(
            action_kind=ActionKind.CANCEL_ORDER.value,
            payload={"broker_order_id": broker_order_id, "symbol": symbol},
            requested_by=requested_by,
            mode=self._effective_mode(),
        )
        return self._approval_summary_from_record(record)

    def request_set_capacity(self, *, new_capacity_krw, rationale, requested_by) -> dict:
        self._require_actor(requested_by, role="requester")
        self._validate_self_distinct(requested_by)
        try:
            nav = Decimal(new_capacity_krw)
        except Exception as exc:
            raise WriteHandlerError(f"invalid new_capacity_krw: {exc}") from exc
        if nav < 0:
            raise WriteHandlerError("new_capacity_krw must be non-negative")
        if not rationale or len(rationale) < 10:
            raise WriteHandlerError("rationale must be at least 10 characters")
        record = self.store.create_request(
            action_kind=ActionKind.SET_CAPACITY.value,
            payload={"new_capacity_krw": str(nav), "rationale": rationale},
            requested_by=requested_by,
            mode=self._effective_mode(),
        )
        return self._approval_summary_from_record(record)

    def request_kill_switch(self, *, reason, requested_by) -> dict:
        self._require_actor(requested_by, role="requester")
        if not reason or len(reason) < 5:
            raise WriteHandlerError("kill_switch reason must be at least 5 chars")
        record = self.store.create_request(
            action_kind=ActionKind.KILL_SWITCH.value,
            payload={"reason": reason},
            requested_by=requested_by,
            mode=self._effective_mode(),
        )
        return self._approval_summary_from_record(record)

    # -- list / get / cancel -------------------------------------------------

    def list_pending_approvals(self, *, requested_by=None, limit=50) -> dict:
        if limit <= 0 or limit > 200:
            raise WriteHandlerError("limit must be in (0, 200]")
        records = self.store.list_by_state(ApprovalState.PROPOSED, limit=limit)
        if requested_by:
            records = [r for r in records if r.requested_by == requested_by]
        return {"count": len(records), "approvals": [self._record_to_dict(r) for r in records]}

    def get_approval_detail(self, *, approval_id) -> dict:
        record = self._get_or_raise(approval_id)
        return self._record_to_dict(record, include_payload=True)

    def cancel_proposed_action(self, *, approval_id, actor,
                                reason="cancelled by requester") -> dict:
        self._require_actor(actor, role="canceller")
        record = self._get_or_raise(approval_id)
        if record.state != ApprovalState.PROPOSED:
            raise WriteHandlerError(f"can only cancel PROPOSED, got {record.state.value}")
        if actor != record.requested_by and actor != self.operator_id:
            raise WriteHandlerError("only the requester or the operator may cancel a proposal")
        # Phase 1: cancel(id, *, cancelled_by=, reason=)
        self.store.cancel(approval_id, cancelled_by=actor, reason=reason)
        return self._approval_summary(approval_id)

    # -- execute_approved_action ---------------------------------------------

    def execute_approved_action(self, *, approval_id, actor) -> dict:
        self._require_actor(actor, role="executor")
        record = self._get_or_raise(approval_id)
        if record.state != ApprovalState.APPROVED:
            if record.state.value in ("executed", "exec_failed"):
                return self._record_to_dict(record, include_payload=True)
            raise WriteHandlerError(
                f"approval state is {record.state.value}; expected APPROVED"
            )
        try:
            kind = record.action_kind if isinstance(record.action_kind, str) else record.action_kind.value
            if kind == ActionKind.SUBMIT_ORDER.value:
                gateway = self._require_gateway("execute_approved_action")
                result = gateway.execute_approved(approval_id, actor=actor)
                return self._execution_result_to_dict(result, record)
            if kind == ActionKind.CANCEL_ORDER.value:
                return self._execute_cancel_order(record, actor)
            if kind == ActionKind.SET_CAPACITY.value:
                return self._execute_set_capacity(record, actor)
            if kind == ActionKind.KILL_SWITCH.value:
                return self._execute_kill_switch(record, actor)
            raise WriteHandlerError(f"unknown action_kind: {kind}")
        except (LiveModeBlockedError, InterruptedExecutionError,
                ExpiredApprovalError, InvalidStateTransitionError) as exc:
            raise WriteHandlerError(str(exc)) from exc

    # -- internal CLI handlers -----------------------------------------------

    def approve_action(self, *, approval_id, decided_by, comment=None) -> dict:
        self._require_actor(decided_by, role="approver")
        try:
            # Phase 1: approve(id, *, decided_by=, reason=)
            self.store.approve(approval_id, decided_by=decided_by,
                               reason=comment)
        except SelfApprovalError as exc:
            raise WriteHandlerError(str(exc)) from exc
        return self._approval_summary(approval_id)

    def reject_action(self, *, approval_id, decided_by, reason) -> dict:
        self._require_actor(decided_by, role="rejecter")
        if not reason:
            raise WriteHandlerError("rejection reason required")
        # Phase 1: reject(id, *, decided_by=, reason=)
        try:
            self.store.reject(approval_id, decided_by=decided_by, reason=reason)
        except Exception as exc:
            raise WriteHandlerError(str(exc)) from exc
        return self._approval_summary(approval_id)

    # -- action-kind executors -----------------------------------------------

    def _execute_cancel_order(self, record, actor) -> dict:
        gateway = self._require_gateway("_execute_cancel_order")
        broker = gateway._broker
        payload = _record_payload(record)
        # Phase 1: mark_executing(id, *, executed_by=)
        self.store.mark_executing(record.approval_id, executed_by=actor)
        try:
            result = broker.cancel_order(
                broker_order_id=payload["broker_order_id"],
                symbol=payload["symbol"],
                approval_id=record.approval_id,
            )
            # Phase 1: mark_executed(id, *, result=)
            self.store.mark_executed(record.approval_id, result={
                "cancelled": result.get("cancelled", False),
                "broker_order_id": payload["broker_order_id"],
                "executed_at_utc": datetime.now(timezone.utc).isoformat(),
            })
        except Exception as exc:
            self.store.mark_exec_failed(record.approval_id, error_message=str(exc))
            raise WriteHandlerError(f"cancel_order failed: {exc}") from exc
        return self._record_to_dict(self._get_or_raise(record.approval_id),
                                     include_payload=True)

    def _execute_set_capacity(self, record, actor) -> dict:
        self.store.mark_executing(record.approval_id, executed_by=actor)
        self.store.mark_executed(record.approval_id, result={
            "applied_at_utc": datetime.now(timezone.utc).isoformat(),
            "new_capacity_krw": _record_payload(record).get("new_capacity_krw"),
        })
        return self._record_to_dict(self._get_or_raise(record.approval_id),
                                     include_payload=True)

    def _execute_kill_switch(self, record, actor) -> dict:
        self.store.mark_executing(record.approval_id, executed_by=actor)
        self.store.mark_executed(record.approval_id, result={
            "kill_switch_armed_at_utc": datetime.now(timezone.utc).isoformat(),
            "reason": _record_payload(record).get("reason"),
        })
        return self._record_to_dict(self._get_or_raise(record.approval_id),
                                     include_payload=True)

    # -- helpers -------------------------------------------------------------

    def _get_or_raise(self, approval_id: str):
        try:
            return self.store.get(approval_id)
        except Exception as exc:
            if "not found" in str(exc).lower():
                raise WriteHandlerError(f"approval not found: {approval_id}")
            raise WriteHandlerError(str(exc)) from exc

    def _approval_summary_from_record(self, record) -> dict:
        expires = getattr(record, "expires_at", None)
        return {
            "approval_id": record.approval_id,
            "state": record.state.value if hasattr(record.state, "value") else str(record.state),
            "action_kind": record.action_kind if isinstance(record.action_kind, str) else record.action_kind.value,
            "requested_by": record.requested_by,
            "mode": getattr(record, "mode", self._effective_mode()),
            "expires_at": expires.isoformat() if expires else None,
        }

    def _approval_summary(self, approval_id: str) -> dict:
        return self._approval_summary_from_record(self._get_or_raise(approval_id))

    def _record_to_dict(self, record, *, include_payload=False) -> dict:
        d = self._approval_summary_from_record(record)
        d.update({
            "decided_by": getattr(record, "decided_by", None),
            "decided_at": getattr(record, "decided_at", None),
            "error_message": getattr(record, "decision_reason", None) or getattr(record, "error_message", None),
            # Phase 2-B: uppercase status alias for tests/CLIs that prefer
            # "EXECUTED"/"APPROVED"/"REJECTED" naming over the lowercase
            # state.value enum string. Both keys are populated for clarity.
            "status": (
                record.state.value.upper()
                if hasattr(record.state, "value") else str(record.state).upper()
            ),
        })
        if include_payload:
            d["action_payload"] = _record_payload(record)
            result = _record_result(record)
            if result is not None:
                d["execution_payload"] = result
                # Phase 2-B: broker_response alias — same content as
                # execution_payload, with `status` auto-derived from
                # `accepted` if not already present. The normalized
                # internal envelope only carries `accepted: bool`, but
                # tests expect `broker_response["status"]` to be
                # "ACCEPTED"/"REJECTED" strings. Defensive copy so
                # callers can't mutate the underlying result map.
                br = dict(result)
                if "status" not in br:
                    br["status"] = "ACCEPTED" if br.get("accepted") else "REJECTED"
                d["broker_response"] = br
        return d

    def _execution_result_to_dict(self, result: ExecutionResult, record) -> dict:
        kind = record.action_kind if isinstance(record.action_kind, str) else record.action_kind.value
        return {
            "approval_id": result.approval_id,
            "success": result.success,
            "state": result.state.value,
            "action_kind": kind,
            "broker_order_id": result.broker_order_id,
            "filled_quantity": str(result.filled_quantity),
            "average_price": str(result.average_price) if result.average_price else None,
            "error_message": result.error_message,
            "executed_at_utc": result.executed_at_utc.isoformat(),
            "elapsed_ms": result.elapsed_ms,
            "mode": getattr(record, "mode", self._effective_mode()),
        }

    @staticmethod
    def _require_actor(actor: str, *, role: str):
        if not actor or not actor.strip():
            raise WriteHandlerError(f"{role} actor id is required")
        if len(actor) > 64:
            raise WriteHandlerError(f"{role} actor id too long (max 64)")

    @staticmethod
    def _validate_self_distinct(actor: str):
        """Block actor IDs that suggest privilege escalation or impersonation.

        Phase 2-B change: `"agent"` was REMOVED from this list. LLM agent
        identifiers commonly include `"agent"` (e.g. as a default/canonical
        name), and integration tests pass `requested_by="agent"`
        intentionally. Allowing it here does NOT weaken self-approval
        protection — `<assumption>` #2 (SelfApprovalError) is still
        enforced at the store level: if both the requester and the
        approver are `"agent"`, the approval fails there.

        The remaining forbidden names (`"operator"`, `"admin"`, `"root"`)
        carry implicit authority semantics; allowing them as requester
        IDs would let a propose call masquerade as a privileged actor
        for downstream audit logs. These remain blocked.

        Empty/whitespace-only is still rejected (defense against
        accidental missing IDs from upstream).
        """
        if actor.strip().lower() in ("", "operator", "admin", "root"):
            raise WriteHandlerError(
                f"actor id {actor!r} is too generic / privileged-looking; "
                f"use a unique role+name id"
            )

    @staticmethod
    def _generate_coid() -> str:
        import uuid
        return f"jcpr-{uuid.uuid4().hex[:12]}"


def build_handlers(*, store, gateway, operator_id,
                   proposal_ttl=300, execution_ttl=60, kill_switch_ttl=60):
    if store is None: raise ValueError("store is required")
    if gateway is None: raise ValueError("gateway is required")
    if not operator_id: raise ValueError("operator_id is required")
    if gateway.store is not store:
        raise ValueError(
            "gateway.store must be the same instance as the provided store "
            "(Phase 2 unified-store invariant)"
        )
    return WriteHandlers(store=store, gateway=gateway, operator_id=operator_id,
                         proposal_ttl=proposal_ttl, execution_ttl=execution_ttl,
                         kill_switch_ttl=kill_switch_ttl)
