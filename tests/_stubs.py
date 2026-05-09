"""In-memory stub — Phase 1 실제 API 완전 일치 (최종 확정판).

확인된 Phase 1 실제 스펙:
ApprovalStore 공개 메서드:
  create_request, get, approve, reject, cancel,
  mark_executing, mark_executed, mark_exec_failed,
  list_by_state, list_pending, expire_overdue
  — close(), list_recent() 없음
  — _closed 속성 없음

ApprovalState 값: 소문자 ('proposed', 'approved', ...)
OrderSide: OrderSide.BUY = 'buy'
OrderType: OrderType.LIMIT = 'limit'

OrderRequest.__init__ 필수 인자:
  symbol, side, order_type, quantity, limit_price_krw,
  client_order_id, strategy_id, approval_id, requested_at_utc

ApprovalRecord 필드:
  approval_id, action_kind(str), payload, requested_by, mode,
  state, created_at, expires_at, decided_by, decided_at,
  decision_reason, execute_expires_at, executed_by, executed_at,
  execution_result
  — action_payload, execution_payload, error_message 없음
"""
from __future__ import annotations
import enum, secrets as _sec, threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping, Optional


# ── Exceptions ────────────────────────────────────────────────────────────────
class ApprovalStoreError(Exception): pass
class ApprovalNotFound(ApprovalStoreError): pass
class ApprovalStateError(ApprovalStoreError): pass
class SelfApprovalError(ApprovalStoreError): pass
class ApprovalExpiredError(ApprovalStoreError): pass
class ApprovalIntegrityError(ApprovalStoreError): pass
class LiveModeBlockedError(ApprovalStoreError): pass


# ── ApprovalState — 소문자 값 ─────────────────────────────────────────────────
class ApprovalState(str, enum.Enum):
    PROPOSED    = "proposed"
    APPROVED    = "approved"
    REJECTED    = "rejected"
    EXECUTING   = "executing"
    EXECUTED    = "executed"
    EXEC_FAILED = "exec_failed"
    EXPIRED     = "expired"
    CANCELLED   = "cancelled"


# ── ApprovalRecord — Phase 1 실제 필드명 ─────────────────────────────────────
@dataclass
class ApprovalRecord:
    approval_id: str
    action_kind: str
    payload: dict
    requested_by: str
    mode: str
    state: ApprovalState
    created_at: datetime
    expires_at: datetime
    decided_by: Optional[str] = None
    decided_at: Optional[datetime] = None
    decision_reason: Optional[str] = None
    execute_expires_at: Optional[datetime] = None
    executed_by: Optional[str] = None
    executed_at: Optional[datetime] = None
    execution_result: Optional[dict] = None


# ── ApprovalStore — Phase 1 실제 메서드만 (close/list_recent/_closed 없음) ────
class ApprovalStore:
    def __init__(self, db_path=None, *, approval_ttl_seconds=300,
                 execute_ttl_seconds=60, kill_switch_ttl_seconds=60):
        self._records: dict[str, ApprovalRecord] = {}
        self._lock = threading.RLock()
        self.db_path = db_path
        self._ttl = approval_ttl_seconds

    def _gen_id(self) -> str:
        return f"apv-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{_sec.token_hex(8)}"

    def create_request(self, *, action_kind: str, payload: dict,
                       requested_by: str, mode: str = "paper",
                       session_id=None, trace_id=None) -> ApprovalRecord:
        with self._lock:
            now = datetime.now(timezone.utc)
            r = ApprovalRecord(
                approval_id=self._gen_id(),
                action_kind=action_kind,
                payload=dict(payload),
                requested_by=requested_by,
                mode=mode,
                state=ApprovalState.PROPOSED,
                created_at=now,
                expires_at=now + timedelta(seconds=self._ttl),
            )
            self._records[r.approval_id] = r
            return r

    def get(self, approval_id: str) -> ApprovalRecord:
        with self._lock:
            r = self._records.get(approval_id)
            if r is None:
                raise ApprovalNotFound(f"approval not found: {approval_id}")
            return r

    def approve(self, approval_id: str, *, decided_by: str,
                reason: Optional[str] = None) -> ApprovalRecord:
        with self._lock:
            r = self.get(approval_id)
            if r.state != ApprovalState.PROPOSED:
                raise ApprovalStateError(f"cannot approve from {r.state.value}")
            if decided_by == r.requested_by:
                raise SelfApprovalError(
                    f"self-approval blocked: {r.requested_by} == {decided_by}")
            r.state = ApprovalState.APPROVED
            r.decided_by = decided_by
            r.decided_at = datetime.now(timezone.utc)
            r.decision_reason = reason
            return r

    def reject(self, approval_id: str, *, decided_by: str,
               reason: Optional[str] = None) -> ApprovalRecord:
        with self._lock:
            r = self.get(approval_id)
            if r.state != ApprovalState.PROPOSED:
                raise ApprovalStateError(f"cannot reject from {r.state.value}")
            r.state = ApprovalState.REJECTED
            r.decided_by = decided_by
            r.decided_at = datetime.now(timezone.utc)
            r.decision_reason = reason
            return r

    def cancel(self, approval_id: str, *, cancelled_by: str,
               reason: Optional[str] = None) -> ApprovalRecord:
        with self._lock:
            r = self.get(approval_id)
            if r.state != ApprovalState.PROPOSED:
                raise ApprovalStateError(f"cannot cancel from {r.state.value}")
            r.state = ApprovalState.CANCELLED
            r.decided_by = cancelled_by
            r.decided_at = datetime.now(timezone.utc)
            r.decision_reason = reason
            return r

    def mark_executing(self, approval_id: str, *, executed_by: str) -> ApprovalRecord:
        with self._lock:
            r = self.get(approval_id)
            if r.state != ApprovalState.APPROVED:
                raise ApprovalStateError(f"cannot → EXECUTING from {r.state.value}")
            r.state = ApprovalState.EXECUTING
            r.executed_by = executed_by
            return r

    def mark_executed(self, approval_id: str, *, result: dict) -> ApprovalRecord:
        with self._lock:
            r = self.get(approval_id)
            if r.state != ApprovalState.EXECUTING:
                raise ApprovalStateError(f"cannot → EXECUTED from {r.state.value}")
            r.state = ApprovalState.EXECUTED
            r.execution_result = dict(result)
            r.executed_at = datetime.now(timezone.utc)
            return r

    def mark_exec_failed(self, approval_id: str, *, error_message: str) -> ApprovalRecord:
        with self._lock:
            r = self.get(approval_id)
            if r.state not in (ApprovalState.EXECUTING, ApprovalState.APPROVED):
                raise ApprovalStateError(f"cannot → EXEC_FAILED from {r.state.value}")
            r.state = ApprovalState.EXEC_FAILED
            r.decision_reason = error_message
            return r

    def list_by_state(self, state: ApprovalState, *, limit: int = 100):
        with self._lock:
            return [r for r in self._records.values() if r.state == state][:limit]

    def list_pending(self, *, limit: int = 100):
        return self.list_by_state(ApprovalState.PROPOSED, limit=limit)

    def expire_overdue(self) -> int:
        return 0


# ── OrderSide / OrderType ─────────────────────────────────────────────────────
class OrderSide(str, enum.Enum):
    BUY  = "buy"
    SELL = "sell"

class OrderType(str, enum.Enum):
    MARKET = "market"
    LIMIT  = "limit"


# ── OrderRequest — Phase 1 실제 시그니처 ─────────────────────────────────────
@dataclass(frozen=True)
class OrderRequest:
    """Phase 1 실제 OrderRequest.__init__ 시그니처:
    symbol, side, order_type, quantity, limit_price_krw,
    client_order_id, strategy_id, approval_id, requested_at_utc
    """
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: Decimal
    limit_price_krw: Optional[Decimal]
    client_order_id: str
    strategy_id: str
    approval_id: str
    requested_at_utc: datetime


# ── OrderResponse ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class OrderResponse:
    accepted: bool
    broker_order_id: Optional[str]
    client_order_id: Optional[str]
    filled_quantity: Decimal
    average_price: Optional[Decimal]
    error_code: Optional[str]
    error_message: Optional[str]
    submitted_at_utc: datetime


class BrokerExecutionInterface:
    def place_order(self, req, *, approval_id): raise NotImplementedError
    def cancel_order(self, *, broker_order_id, symbol, approval_id): raise NotImplementedError


class MockBroker(BrokerExecutionInterface):
    def __init__(self, *, accepted=True, broker_order_id="B-12345",
                 filled_quantity=Decimal("10"), average_price=Decimal("75000"),
                 error_code=None, error_message=None, raise_exception=None):
        self.accepted = accepted
        self.broker_order_id = broker_order_id
        self.filled_quantity = filled_quantity
        self.average_price = average_price
        self.error_code = error_code
        self.error_message = error_message
        self.raise_exception = raise_exception
        self.place_order_calls = []
        self.cancel_order_calls = []

    def place_order(self, req, *, approval_id):
        self.place_order_calls.append((req, approval_id))
        if self.raise_exception:
            raise self.raise_exception
        return OrderResponse(
            accepted=self.accepted,
            broker_order_id=self.broker_order_id if self.accepted else None,
            client_order_id=req.client_order_id,
            filled_quantity=self.filled_quantity if self.accepted else Decimal("0"),
            average_price=self.average_price if self.accepted else None,
            error_code=self.error_code,
            error_message=self.error_message,
            submitted_at_utc=datetime.now(timezone.utc),
        )

    def cancel_order(self, *, broker_order_id, symbol, approval_id):
        self.cancel_order_calls.append({"broker_order_id": broker_order_id})
        return {"cancelled": True}
