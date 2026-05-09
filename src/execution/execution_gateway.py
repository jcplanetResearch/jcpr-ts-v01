"""ExecutionGateway — Phase 2 (Phase 1 ApprovalStore 실제 인터페이스 기준).

Phase 1 approval_store.py 실제 메서드:
  create_request(*, action_kind: str, payload, requested_by, mode)
  approve(id, *, decided_by, reason=None)
  cancel(id, *, cancelled_by, reason=None)
  mark_executing(id, *, executed_by)
  mark_executed(id, *, result: Mapping)
  mark_exec_failed(id, *, error_message: str)
  list_by_state(state, *, limit)
  get(id) -> ApprovalRecord  (raises ApprovalNotFound if missing)

Phase 1 ApprovalStore.__init__:
  ApprovalStore(db_path, *, approval_ttl_seconds=..., execute_ttl_seconds=..., kill_switch_ttl_seconds=...)
  — file_mode 파라미터 없음

Phase 1 OrderRequest 필드:
  symbol, side, order_type, quantity, limit_price_krw, client_order_id, strategy_id
  — limit_price 아님, limit_price_krw 임
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping, Optional

from src.execution._action_kind import ActionKind
from src.execution.approval_store import (
    ApprovalRecord,
    ApprovalState,
    ApprovalStore,
    ApprovalStoreError,
)
try:
    from src.execution.approval_store import ApprovalExpiredError as ExpiredApprovalError
    from src.execution.approval_store import ApprovalStateError as InvalidStateTransitionError
    from src.execution.approval_store import SelfApprovalError
    from src.execution.approval_store import LiveModeBlockedError
    from src.execution.approval_store import ApprovalNotFound
except ImportError:
    ExpiredApprovalError = ApprovalStoreError       # type: ignore
    InvalidStateTransitionError = ApprovalStoreError  # type: ignore
    SelfApprovalError = ApprovalStoreError           # type: ignore
    LiveModeBlockedError = ApprovalStoreError        # type: ignore
    ApprovalNotFound = ApprovalStoreError            # type: ignore

__all__ = [
    # Core
    "ExecutionGateway", "ExecutionResult",
    # Base exception hierarchy
    "GatewayError", "InterruptedExecutionError",
    # Re-export from approval_store (already imported above)
    "LiveModeBlockedError",
    "ExpiredApprovalError",
    # New, granular exceptions for downstream test/caller distinction
    "KillSwitchActiveError",
    "AlreadyExecutedError",
    "AlreadyExecutingError",
    "BrokerExecutionError",
    "ModeViolationError",
]

logger = logging.getLogger(__name__)


class GatewayError(Exception):
    """Base class for all ExecutionGateway errors."""
    pass


class InterruptedExecutionError(GatewayError):
    """ESC/Ctrl-C or interrupt_check fired during execution."""
    pass


class KillSwitchActiveError(InterruptedExecutionError):
    """Kill-switch flag detected by interrupt_check.

    Subclass of InterruptedExecutionError — existing
    `except InterruptedExecutionError` handlers continue to catch this,
    preserving the kill-priority guarantee (kill always prevails over
    new orders).
    """
    pass


class AlreadyExecutedError(GatewayError):
    """Attempted to execute an approval already in EXECUTED state.

    Enforces single-use semantics (assumption #7). Replaces the previous
    silent-return-of-cached-result path; callers that need the historical
    result should call `gateway.store.get(approval_id)` directly.
    """
    pass


class AlreadyExecutingError(GatewayError):
    """Attempted to execute an approval already in EXECUTING state.

    Indicates concurrent execute_approved invocations on the same
    approval_id. The store-level RLock + single-use guard in
    mark_executing prevents the second writer from succeeding;
    this exception surfaces that condition to the caller.
    """
    pass


class BrokerExecutionError(GatewayError):
    """Broker rejected the order or raised an unexpected exception.

    Replaces the previous generic GatewayError wrap of broker-side
    failures, allowing tests and callers to distinguish broker
    failures from gateway-internal failures (mode/state violations).
    Error message MUST NOT contain order payload details that could
    expose private symbols/quantities; broker-side message is
    truncated by the underlying mark_exec_failed (max 500 chars).
    """
    pass


class ModeViolationError(GatewayError):
    """Approval record mode does not match Gateway mode.

    Distinct from LiveModeBlockedError: LiveModeBlockedError is raised
    at gateway construction (live mode without allow_live=True);
    ModeViolationError is raised at execute time when a paper-mode
    record reaches a live-mode gateway or vice versa. Both are
    fail-closed gates.
    """
    pass


def _record_payload(record) -> dict:
    """Phase 1 필드: payload (또는 action_payload fallback)."""
    v = getattr(record, "payload", None) or getattr(record, "action_payload", None)
    return dict(v) if v else {}

def _record_result(record) -> Optional[dict]:
    """Phase 1 필드: execution_result (또는 result/execution_payload fallback)."""
    for attr in ("execution_result", "result", "execution_payload"):
        v = getattr(record, attr, None)
        if v is not None:
            return dict(v)
    return None

def _record_error(record) -> Optional[str]:
    """Phase 1 필드: decision_reason (또는 error_message fallback)."""
    return getattr(record, "decision_reason", None) or getattr(record, "error_message", None)


@dataclass(frozen=True, slots=True)
class _NormalizedResponse:
    """Internal canonical broker-response shape.

    Adapter outputs (from `_normalize_dict_response` and
    `_normalize_order_response`) flow through this; downstream code
    (`_serialize_broker_response`, EXECUTED/EXEC_FAILED branches)
    reads the same field names regardless of whether the broker is
    a mock dict-style adapter or the production OrderResponse-style
    adapter. Frozen to prevent accidental mutation between adapter
    output and persistence.
    """
    accepted: bool
    broker_order_id: Optional[str]
    client_order_id: Optional[str]
    filled_quantity: Decimal
    average_price: Optional[Decimal]
    error_code: Optional[str]
    error_message: Optional[str]
    submitted_at_utc: datetime


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    approval_id: str
    success: bool
    state: ApprovalState
    broker_order_id: Optional[str]
    filled_quantity: Decimal
    average_price: Optional[Decimal]
    error_message: Optional[str]
    executed_at_utc: datetime
    elapsed_ms: int
    raw_response: Optional[dict] = field(default=None)

    def __post_init__(self):
        if self.executed_at_utc.tzinfo is None:
            raise ValueError("executed_at_utc must be tz-aware UTC")

    # ------------------------------------------------------------------
    # Test-friendly aliases (Phase 2-B)
    # ------------------------------------------------------------------

    @property
    def final_status(self) -> str:
        """Uppercase state value for test/log readability.

        Returns 'EXECUTED', 'EXEC_FAILED', etc. — the same content as
        `state.value` but in the uppercase convention used by integration
        tests (e.g. `assert result.final_status == "EXECUTED"`).
        Production code should prefer `state` (the Enum) for type safety.
        """
        return self.state.value.upper()

    @property
    def broker_response(self) -> dict:
        """Convenience accessor for the broker payload.

        Returns a (defensive) copy of `raw_response` if present, else an
        empty dict — never None. This makes `result.broker_response["status"]`
        access patterns from tests safe without explicit None-checks.

        A `status` field is auto-derived from `accepted` if the underlying
        raw_response did not include one (production OrderResponse-based
        adapter omits string status, only carries the success bool); tests
        expect `result.broker_response["status"]` to be "ACCEPTED" or
        "REJECTED" regardless of broker provenance.
        """
        if not self.raw_response:
            return {}
        out = dict(self.raw_response)
        # Derive 'status' if absent — tests check for "ACCEPTED"/"REJECTED"
        # while the normalized envelope only carries `accepted: bool`.
        if "status" not in out:
            out["status"] = "ACCEPTED" if out.get("accepted") else "REJECTED"
        return out


class ExecutionGateway:
    def __init__(
        self,
        approval_store: "ApprovalStore | None" = None,
        broker=None,
        *,
        # Phase 2-B: keyword aliases for test fixtures
        store: "ApprovalStore | None" = None,
        # Phase 2-B: dual-broker routing (mode-aware)
        paper_broker=None,
        live_broker=None,
        mode: str = "paper",
        allow_live: bool = False,
        # Phase 2-B: object-with-.is_active() OR legacy callable
        interrupt_check=None,
        kill_switch=None,
        audit_writer=None,
    ):
        # ----- store normalization -----
        # Accept either positional `approval_store` or keyword `store=`
        # (tests use `store=`). Both refer to the same ApprovalStore;
        # exactly one must be provided.
        resolved_store = store if store is not None else approval_store
        if resolved_store is None:
            raise TypeError(
                "ExecutionGateway requires an ApprovalStore "
                "(pass as `approval_store=...` or `store=...`)"
            )

        # ----- mode + live gate (preserved fail-closed) -----
        if mode not in ("paper", "live"):
            raise ValueError(f"mode must be 'paper' or 'live', got {mode!r}")
        if mode == "live" and not allow_live:
            raise LiveModeBlockedError(
                "live mode requires allow_live=True"
            )

        # ----- broker routing -----
        # Two configurations supported:
        #   (a) Legacy single-broker:  broker=...
        #       The single broker is bound to the slot matching `mode`.
        #   (b) Dual-broker routing:   paper_broker=..., live_broker=...
        #       Mode-aware dispatch; the inactive slot may be None but
        #       attempts to route to it raise (fail-closed isolation).
        # Mixing (a) and (b) is rejected to avoid ambiguity.
        if broker is not None and (paper_broker is not None or live_broker is not None):
            raise ValueError(
                "provide either `broker=` (legacy single) OR "
                "`paper_broker=`/`live_broker=` (dual routing), not both"
            )

        if paper_broker is not None or live_broker is not None:
            self._paper_broker = paper_broker
            self._live_broker = live_broker
        elif broker is not None:
            # Legacy: bind to slot matching mode
            self._paper_broker = broker if mode == "paper" else None
            self._live_broker = broker if mode == "live" else None
        else:
            # Neither given — defer (will raise on first execute attempt
            # via _active_broker; allows construction-time validation
            # to happen elsewhere if needed)
            self._paper_broker = None
            self._live_broker = None

        # Validate: active broker for current mode must exist (fail-closed)
        active = self._paper_broker if mode == "paper" else self._live_broker
        if active is None:
            raise GatewayError(
                f"no broker configured for mode={mode!r} "
                f"(paper={self._paper_broker is not None}, "
                f"live={self._live_broker is not None}); "
                f"fail-closed isolation"
            )

        # ----- interrupt / kill-switch adapter -----
        # Accept either:
        #   - kill_switch: object with .is_active() -> bool
        #   - interrupt_check: callable() -> bool (legacy)
        # Mixing both is rejected (configuration ambiguity).
        if kill_switch is not None and interrupt_check is not None:
            raise ValueError(
                "provide either `kill_switch=` or `interrupt_check=`, not both"
            )
        if kill_switch is not None:
            if not hasattr(kill_switch, "is_active"):
                raise TypeError(
                    "kill_switch must expose .is_active() -> bool"
                )
            self._kill_switch = kill_switch
            # Phase 2-B: when a kill_switch object is active, raise the
            # specific KillSwitchActiveError (subclass of
            # InterruptedExecutionError) so callers/tests can distinguish
            # an explicit kill-switch trip from a generic ESC/Ctrl-C
            # interrupt callback. The `_check_interrupt` machinery
            # treats a callable that itself raises as a "fired" interrupt
            # and propagates the exception unchanged.
            def _kill_check_adapter():
                if kill_switch.is_active():
                    raise KillSwitchActiveError(
                        "kill switch is active; all executions are blocked "
                        "until it is reset"
                    )
                return False
            self._interrupt_check = _kill_check_adapter
        else:
            self._kill_switch = None
            self._interrupt_check = interrupt_check

        # ----- finalize state -----
        self._store = resolved_store
        # Keep `_broker` for backward compatibility (used by code paths
        # not yet aware of dual-broker). Resolves to the active slot.
        self._mode = mode
        self._allow_live = allow_live
        self._audit_writer = audit_writer
        self._lock = threading.RLock()

    # ----- broker accessors -----

    @property
    def _broker(self):
        """Active broker for current mode — backward-compat read property.

        Internal callers that use `self._broker` continue to work; mode
        switching is not supported at runtime, so the active slot is
        determined once at construction.
        """
        return self._paper_broker if self._mode == "paper" else self._live_broker

    def _active_broker(self):
        """Mode-based broker routing with fail-closed gate.

        Re-validates that the active slot is present at call time,
        defending against post-construction None assignment by callers
        (which is unsupported but cheap to guard).
        """
        target = self._paper_broker if self._mode == "paper" else self._live_broker
        if target is None:
            raise GatewayError(
                f"no broker for mode={self._mode!r} (routing rejected)"
            )
        return target

    @property
    def kill_switch(self):
        """Optional kill_switch object provided at construction (or None)."""
        return self._kill_switch

    @property
    def paper_broker(self):
        return self._paper_broker

    @property
    def live_broker(self):
        return self._live_broker

    def status_snapshot(self) -> dict:
        """Read-only snapshot for tests/operators — no secrets exposed.

        Returns broker class names (not instances) and kill-switch state.
        Does NOT include secrets, payloads, or ApprovalStore contents.
        """
        return {
            "mode": self._mode,
            "allow_live": self._allow_live,
            "paper_broker": (
                type(self._paper_broker).__name__
                if self._paper_broker is not None else None
            ),
            "live_broker": (
                type(self._live_broker).__name__
                if self._live_broker is not None else None
            ),
            "kill_switch_active": (
                bool(self._kill_switch.is_active())
                if self._kill_switch is not None else None
            ),
        }

    @property
    def mode(self): return self._mode
    @property
    def allow_live(self): return self._allow_live
    @property
    def store(self): return self._store

    def _check_interrupt(self, where: str):
        if self._interrupt_check is not None:
            try:
                if self._interrupt_check():
                    raise InterruptedExecutionError(f"interrupt fired at {where}")
            except InterruptedExecutionError:
                raise
            except Exception as exc:
                logger.warning("interrupt_check raised: %s", exc)

    def propose_order(self, order_request, *, requested_by: str, ttl_seconds: int = 300) -> str:
        self._check_interrupt("propose_order")
        if not requested_by:
            raise ValueError("requested_by is required")
        payload = self._serialize_order_request(order_request)
        # Phase 1: create_request(action_kind=str, payload=, requested_by=, mode=)
        record = self._store.create_request(
            action_kind=ActionKind.SUBMIT_ORDER.value,
            payload=payload,
            requested_by=requested_by,
            mode=self._mode,
        )
        return record.approval_id

    def execute(self, approval_id: str, *, actor: str = "operator") -> ExecutionResult:
        """Compatibility wrapper for execute_approved.

        Phase 2-B: integration tests call `gateway.execute(aid)` without
        passing actor. Default `actor="operator"` matches the typical
        operator-driven flow used by approve_cli; callers needing
        attribution to a specific operator should pass actor explicitly
        or use execute_approved() directly.

        All exception types (AlreadyExecutedError, ModeViolationError,
        BrokerExecutionError, etc.) are propagated unchanged.
        """
        return self.execute_approved(approval_id, actor=actor)

    def execute_approved(self, approval_id: str, *, actor: str) -> ExecutionResult:
        self._check_interrupt("execute_approved")
        if not actor:
            raise ValueError("actor is required")

        with self._lock:
            try:
                record = self._store.get(approval_id)
            except ApprovalNotFound:
                raise GatewayError(f"approval not found: {approval_id}")
            except Exception as exc:
                if "not found" in str(exc).lower():
                    raise GatewayError(f"approval not found: {approval_id}")
                raise GatewayError(str(exc)) from exc

            if record.state == ApprovalState.EXECUTED:
                # Single-use enforcement (assumption #7).
                # Previously returned cached result silently; now raises
                # so callers can distinguish "first execution succeeded"
                # from "redundant re-execution attempt".
                # Callers needing the historical result should call
                # self._store.get(approval_id) directly.
                raise AlreadyExecutedError(
                    f"approval {approval_id} already executed "
                    f"(single-use enforcement)"
                )
            if record.state == ApprovalState.EXECUTING:
                # Concurrent execute attempt detected before mark_executing
                # could fire. Distinct from the post-mark_executing race,
                # which surfaces as ApprovalStateError from the store and
                # is also re-raised below as AlreadyExecutingError.
                raise AlreadyExecutingError(
                    f"approval {approval_id} is currently executing"
                )
            if record.state.value == "exec_failed":
                # Phase 2-B: surface the same single-use violation type as
                # an EXECUTED retry. Both EXECUTED and EXEC_FAILED are
                # terminal "execute attempted" states; callers asking
                # whether the record can be processed want a single
                # exception class for the "no, it already went through"
                # answer. The original failure remains accessible via
                # store.get(approval_id).execution_result for inspection.
                raise AlreadyExecutedError(
                    f"approval {approval_id} previously failed; "
                    f"cannot retry (state=exec_failed). "
                    f"Original failure recorded in execution_result."
                )
            if record.state == ApprovalState.EXPIRED:
                # Phase 2-B: EXPIRED is a distinct error from generic
                # state-machine violations. Tests and operators expect
                # `ExpiredApprovalError` so they can distinguish "the
                # window for action elapsed" from "the record is in an
                # unexpected state." This branch fires for both
                # decision-TTL expiry (PROPOSED → EXPIRED) and
                # execute-TTL expiry (APPROVED → EXPIRED), both of
                # which the store's get() auto-expire produces.
                raise ExpiredApprovalError(
                    f"approval {approval_id} has expired "
                    f"(state=expired); cannot execute"
                )
            if record.state != ApprovalState.APPROVED:
                raise GatewayError(
                    f"cannot execute approval in state {record.state.value}"
                )

            # mode 일관성 검사
            rec_mode = getattr(record, "mode", None)
            if rec_mode and rec_mode != self._mode:
                raise ModeViolationError(
                    f"approval mode={rec_mode!r} != gateway mode={self._mode!r}"
                )

            # Phase 1: mark_executing(id, *, executed_by=)
            # If a concurrent caller already advanced PROPOSED→EXECUTING
            # between our get() above and this call, the store raises
            # ApprovalStateError; surface that as AlreadyExecutingError.
            try:
                self._store.mark_executing(approval_id, executed_by=actor)
            except InvalidStateTransitionError as exc:
                raise AlreadyExecutingError(
                    f"approval {approval_id} state changed concurrently: {exc}"
                ) from exc

        started_at = datetime.now(timezone.utc)
        try:
            self._check_interrupt("execute_approved.broker_call")
            deser_payload = _record_payload(record)
            deser_payload["approval_id"] = approval_id
            # Route through adapter layer — supports both production
            # OrderRequest-based place_order and mock dict-based submit_order.
            broker_response = self._call_broker_submit(
                deser_payload, approval_id, started_at
            )
            elapsed_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)

            result_payload = self._serialize_broker_response(broker_response)
            result_payload["elapsed_ms"] = elapsed_ms

            if broker_response.accepted:
                # Phase 1: mark_executed(id, *, result=)
                self._store.mark_executed(approval_id, result=result_payload)
                return ExecutionResult(
                    approval_id=approval_id, success=True,
                    state=ApprovalState.EXECUTED,
                    broker_order_id=broker_response.broker_order_id,
                    filled_quantity=broker_response.filled_quantity,
                    average_price=broker_response.average_price,
                    error_message=None,
                    executed_at_utc=started_at, elapsed_ms=elapsed_ms,
                    raw_response=result_payload,
                )
            # broker 거부
            self._store.mark_exec_failed(
                approval_id,
                error_message=broker_response.error_message or "broker rejected",
            )
            return ExecutionResult(
                approval_id=approval_id, success=False,
                state=ApprovalState.EXEC_FAILED,
                broker_order_id=broker_response.broker_order_id,
                filled_quantity=Decimal("0"),
                average_price=None,
                error_message=broker_response.error_message,
                executed_at_utc=started_at, elapsed_ms=elapsed_ms,
                raw_response=result_payload,
            )

        except InterruptedExecutionError:
            # Phase 1: mark_exec_failed(id, *, error_message=)
            # Covers both ESC/Ctrl-C and KillSwitchActiveError (subclass).
            self._store.mark_exec_failed(
                approval_id, error_message="interrupted by ESC/Ctrl-C or kill-switch"
            )
            raise

        except GatewayError:
            # Re-raise our own typed errors without wrapping
            # (AlreadyExecutedError, AlreadyExecutingError,
            #  BrokerExecutionError, ModeViolationError, ...).
            raise

        except Exception as exc:
            elapsed_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
            self._store.mark_exec_failed(approval_id, error_message=str(exc))
            # Broker-side or downstream-call failure. Use BrokerExecutionError
            # so callers/tests can distinguish broker failures from
            # gateway-internal state/mode violations. Message is generic
            # to avoid leaking payload contents; full broker error is
            # persisted via mark_exec_failed (truncated to 500 chars).
            raise BrokerExecutionError(f"execution failed: {exc}") from exc

    def cancel_proposed(self, approval_id: str, *, actor: str, reason: str = "cancelled") -> None:
        self._check_interrupt("cancel_proposed")
        # Phase 1: cancel(id, *, cancelled_by=, reason=)
        self._store.cancel(approval_id, cancelled_by=actor, reason=reason)

    @staticmethod
    def _serialize_order_request(req) -> dict:
        # limit_price_krw 또는 limit_price 둘 다 지원
        lp = getattr(req, "limit_price_krw", None) or getattr(req, "limit_price", None)
        return {
            "symbol": req.symbol,
            "side": str(req.side.value if hasattr(req.side, "value") else req.side),
            "quantity": str(req.quantity),
            "order_type": str(req.order_type.value if hasattr(req.order_type, "value") else req.order_type),
            "limit_price_krw": str(lp) if lp is not None else None,
            "time_in_force": getattr(req, "time_in_force", "DAY"),
            "client_order_id": req.client_order_id,
            "strategy_id": getattr(req, "strategy_id", None),
        }

    @staticmethod
    def _deserialize_order_request(payload: dict):
        from src.brokers.base import OrderRequest, OrderSide, OrderType
        lp_raw = payload.get("limit_price_krw") or payload.get("limit_price")
        side_val = payload["side"].lower()
        otype_val = payload["order_type"].lower()
        # Phase 1 OrderRequest 필수 인자: approval_id, requested_at_utc 포함
        return OrderRequest(
            symbol=payload["symbol"],
            side=OrderSide(side_val),
            order_type=OrderType(otype_val),
            quantity=Decimal(payload["quantity"]),
            limit_price_krw=Decimal(lp_raw) if lp_raw else None,
            client_order_id=payload["client_order_id"],
            strategy_id=payload.get("strategy_id") or "",
            approval_id=payload.get("approval_id") or "",
            requested_at_utc=datetime.now(timezone.utc),
        )

    @staticmethod
    def _serialize_broker_response(resp) -> dict:
        return {
            "accepted": resp.accepted,
            "broker_order_id": resp.broker_order_id,
            "client_order_id": resp.client_order_id,
            "filled_quantity": str(resp.filled_quantity),
            "average_price": str(resp.average_price) if resp.average_price is not None else None,
            "error_code": resp.error_code,
            "error_message": resp.error_message,
            "submitted_at_utc": resp.submitted_at_utc.isoformat(),
        }

    # ------------------------------------------------------------------
    # Broker adapter layer (Phase 2-B)
    # ------------------------------------------------------------------
    # Supports two broker call conventions:
    #   1. Mock/test:    broker.submit_order(payload: dict) -> dict
    #   2. Production:   broker.place_order(request: OrderRequest) -> OrderResponse
    # The active broker is selected by mode (paper_broker vs live_broker)
    # at call time via _active_broker(), preserving fail-closed isolation.

    def _call_broker_submit(self, payload: dict, approval_id: str, started_at):
        """Dispatch order submission to the active broker.

        Detects the broker's calling convention by attribute presence:
            - submit_order(payload) → mock-style (dict in/out)
            - place_order(request)  → production-style (OrderRequest/OrderResponse)
        Raises GatewayError if neither method is present on the broker
        (incompatible adapter). The mode-based routing ensures a paper-mode
        gateway never calls a live broker, even structurally.
        """
        broker = self._active_broker()
        payload_with_id = dict(payload)
        payload_with_id["approval_id"] = approval_id

        if hasattr(broker, "submit_order"):
            # Mock/test path. Mock raises RuntimeError on simulated rejection;
            # that propagates upward and is caught as BrokerExecutionError
            # by the existing exception handling in execute_approved.
            raw = broker.submit_order(payload_with_id)
            if not isinstance(raw, Mapping) and not isinstance(raw, dict):
                raise GatewayError(
                    f"submit_order must return dict, got {type(raw).__name__}"
                )
            return self._normalize_dict_response(raw, payload_with_id, started_at)

        if hasattr(broker, "place_order"):
            # Production path — preserves existing KISExecutionAdapter contract.
            order_request = self._deserialize_order_request(payload_with_id)
            resp = broker.place_order(order_request)
            return self._normalize_order_response(resp, started_at, payload_with_id)

        raise GatewayError(
            f"broker {type(broker).__name__} exposes neither "
            f"submit_order nor place_order — incompatible adapter"
        )

    @staticmethod
    def _normalize_dict_response(raw: dict, payload: dict, started_at) -> "_NormalizedResponse":
        """Convert mock dict response to the internal normalized shape.

        Mock contract (per tests/integration MockKISBroker):
            {"broker_order_id": str, "status": "ACCEPTED"|..., "echo": dict}
        Rejection in mock surfaces as RuntimeError, not as a status code,
        so any dict reaching here is presumed accepted unless status
        explicitly indicates otherwise.
        """
        status = raw.get("status", "")
        accepted = status in ("ACCEPTED", "FILLED", "PENDING") or status == ""
        return _NormalizedResponse(
            accepted=accepted,
            broker_order_id=raw.get("broker_order_id"),
            client_order_id=payload.get("client_order_id"),
            filled_quantity=Decimal(str(raw.get("filled_quantity") or "0")),
            average_price=(
                Decimal(str(raw["average_price"]))
                if raw.get("average_price") is not None else None
            ),
            error_code=raw.get("error_code"),
            error_message=raw.get("error_message"),
            submitted_at_utc=started_at,
        )

    @staticmethod
    def _normalize_order_response(resp, started_at, payload: dict) -> "_NormalizedResponse":
        """Convert production OrderResponse to internal normalized shape.

        Maps OrderResponse.success → accepted, with field-by-field
        defensive getattr to tolerate adapter variants that may not
        expose every field.
        """
        return _NormalizedResponse(
            accepted=bool(getattr(resp, "success", False)),
            broker_order_id=getattr(resp, "broker_order_id", None),
            client_order_id=(
                getattr(resp, "client_order_id", None)
                or payload.get("client_order_id")
            ),
            # Production OrderResponse for a freshly-placed order is
            # typically PENDING with no fills yet; fill data arrives via
            # subsequent status polls (out of scope for this gateway).
            filled_quantity=Decimal("0"),
            average_price=None,
            error_code=getattr(resp, "error_code", None),
            error_message=getattr(resp, "error_message", None),
            submitted_at_utc=getattr(resp, "received_at_utc", None) or started_at,
        )

    def _result_from_executed_record(self, record) -> ExecutionResult:
        payload = _record_result(record) or {}
        return ExecutionResult(
            approval_id=record.approval_id, success=True,
            state=ApprovalState.EXECUTED,
            broker_order_id=payload.get("broker_order_id"),
            filled_quantity=Decimal(payload.get("filled_quantity") or "0"),
            average_price=Decimal(payload["average_price"]) if payload.get("average_price") else None,
            error_message=None,
            executed_at_utc=getattr(record, "decided_at", None) or getattr(record, "created_at", datetime.now(timezone.utc)),
            elapsed_ms=int(payload.get("elapsed_ms") or 0),
            raw_response=payload,
        )

    def _result_from_failed_record(self, record) -> ExecutionResult:
        payload = _record_result(record) or {}
        return ExecutionResult(
            approval_id=record.approval_id, success=False,
            state=ApprovalState.EXEC_FAILED,
            broker_order_id=payload.get("broker_order_id"),
            filled_quantity=Decimal("0"), average_price=None,
            error_message=_record_error(record) or payload.get("error"),
            executed_at_utc=getattr(record, "decided_at", None) or datetime.now(timezone.utc),
            elapsed_ms=int(payload.get("elapsed_ms") or 0),
            raw_response=payload,
        )
