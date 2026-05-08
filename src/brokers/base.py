"""Task 9 — Broker adapter abstract interface.

Defines two contracts:

1. **BrokerAdapter** — read-only broker operations (account, positions, orders).
   Used by Task 9 scripts (check_broker_connection, show_positions, show_orders).

2. **BrokerExecutionInterface** — write operations (place/cancel orders).
   Defined here but **NOT used by Task 9**. Task 40 (approval workflow) is the
   only place that may call these methods, after operator approval. Defining
   this interface in Task 9 makes the Task 40 contract explicit.

Security guarantees:
    - All methods return frozen dataclasses (no mutable state leaks).
    - Decimal-only for any monetary values.
    - UTC tz-aware datetimes.
    - No credentials passed via method args — adapter holds them internally.
    - Adapter mode is immutable: 'paper' or 'prod' fixed at construction.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping, Sequence


# =============================================================================
# Enums and constants
# =============================================================================

class BrokerMode(str, Enum):
    """Broker connection mode. Paper is default for safety."""
    PAPER = "paper"
    PROD = "prod"


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(str, Enum):
    PENDING = "pending"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


# =============================================================================
# Frozen data structures — immutable broker responses
# =============================================================================

@dataclass(frozen=True, slots=True)
class AccountSummary:
    """Account balance snapshot. Currency = KRW for KIS domestic."""
    account_id_masked: str
    cash_balance_krw: Decimal
    total_equity_krw: Decimal
    buying_power_krw: Decimal
    mode: BrokerMode
    fetched_at_utc: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.cash_balance_krw, Decimal):
            raise TypeError("cash_balance_krw must be Decimal")
        if not isinstance(self.total_equity_krw, Decimal):
            raise TypeError("total_equity_krw must be Decimal")
        if not isinstance(self.buying_power_krw, Decimal):
            raise TypeError("buying_power_krw must be Decimal")
        if self.fetched_at_utc.tzinfo is None:
            raise ValueError("fetched_at_utc must be tz-aware (UTC)")


@dataclass(frozen=True, slots=True)
class Position:
    """Single position in the portfolio."""
    symbol: str
    quantity: Decimal
    avg_cost_krw: Decimal
    current_price_krw: Decimal
    market_value_krw: Decimal
    unrealized_pnl_krw: Decimal

    def __post_init__(self) -> None:
        if not self.symbol or not isinstance(self.symbol, str):
            raise ValueError("symbol must be non-empty string")
        for name in ("quantity", "avg_cost_krw", "current_price_krw",
                     "market_value_krw", "unrealized_pnl_krw"):
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                raise TypeError(f"{name} must be Decimal")


@dataclass(frozen=True, slots=True)
class Order:
    """A broker order record (read-only view)."""
    order_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: Decimal
    filled_quantity: Decimal
    limit_price_krw: Decimal | None
    avg_fill_price_krw: Decimal | None
    status: OrderStatus
    placed_at_utc: datetime
    last_updated_utc: datetime

    def __post_init__(self) -> None:
        if not self.order_id or not isinstance(self.order_id, str):
            raise ValueError("order_id must be non-empty string")
        if not isinstance(self.quantity, Decimal):
            raise TypeError("quantity must be Decimal")
        if not isinstance(self.filled_quantity, Decimal):
            raise TypeError("filled_quantity must be Decimal")
        if self.placed_at_utc.tzinfo is None:
            raise ValueError("placed_at_utc must be tz-aware (UTC)")


@dataclass(frozen=True, slots=True)
class ConnectionCheck:
    """Result of a broker connectivity check."""
    success: bool
    mode: BrokerMode
    base_url: str
    tls_version: str
    token_valid: bool
    token_expires_at_utc: datetime | None
    server_time_utc: datetime | None
    error_message: str | None
    elapsed_ms: int

    def __post_init__(self) -> None:
        if self.token_expires_at_utc is not None:
            if self.token_expires_at_utc.tzinfo is None:
                raise ValueError("token_expires_at_utc must be tz-aware")


# =============================================================================
# BrokerAdapter — abstract base class (read-only operations)
# =============================================================================

class BrokerAdapter(ABC):
    """Abstract broker adapter. Read-only operations only.

    Concrete implementations (e.g. KISBrokerAdapter) must:
        1. Hold credentials internally (never accept them as method args).
        2. Set self._mode at __init__ and never change it.
        3. Use TLS 1.2+ for all HTTP calls.
        4. Mask credentials in any log output.
    """

    @property
    @abstractmethod
    def mode(self) -> BrokerMode:
        """Returns the broker mode (paper or prod). Set at __init__."""
        ...

    @property
    @abstractmethod
    def adapter_name(self) -> str:
        """Adapter identifier (e.g. 'kis')."""
        ...

    @abstractmethod
    def check_connection(self) -> ConnectionCheck:
        """Verify connectivity, TLS version, token validity, server time.

        Must NOT raise on connection failure — return ConnectionCheck with
        success=False instead. Only raises for programming errors.
        """
        ...

    @abstractmethod
    def get_account_summary(self) -> AccountSummary:
        """Fetch cash balance, total equity, buying power."""
        ...

    @abstractmethod
    def get_positions(self) -> tuple[Position, ...]:
        """Fetch all current positions (long + short)."""
        ...

    @abstractmethod
    def get_orders(
        self,
        *,
        status: OrderStatus | None = None,
        symbol: str | None = None,
        limit: int = 50,
    ) -> tuple[Order, ...]:
        """Fetch order history. Filter by status and/or symbol."""
        ...


# =============================================================================
# BrokerExecutionInterface — write operations (Task 40 only)
# =============================================================================

@dataclass(frozen=True, slots=True)
class OrderRequest:
    """Order placement request. Constructed by Task 40 ExecutionGateway."""
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: Decimal
    limit_price_krw: Decimal | None
    client_order_id: str       # idempotency key (from Task 22)
    strategy_id: str
    approval_id: str           # Task 40 approval reference
    requested_at_utc: datetime

    def __post_init__(self) -> None:
        if not self.symbol or not isinstance(self.symbol, str):
            raise ValueError("symbol must be non-empty")
        if not isinstance(self.quantity, Decimal):
            raise TypeError("quantity must be Decimal")
        if self.quantity <= Decimal("0"):
            raise ValueError("quantity must be positive")
        if self.order_type == OrderType.LIMIT:
            if self.limit_price_krw is None:
                raise ValueError("limit_price_krw required for LIMIT orders")
            if not isinstance(self.limit_price_krw, Decimal):
                raise TypeError("limit_price_krw must be Decimal")
            if self.limit_price_krw <= Decimal("0"):
                raise ValueError("limit_price_krw must be positive")
        if not self.client_order_id or len(self.client_order_id) > 80:
            raise ValueError("client_order_id must be 1..80 chars")
        if not self.approval_id:
            raise ValueError("approval_id required — Task 40 approval reference")
        if self.requested_at_utc.tzinfo is None:
            raise ValueError("requested_at_utc must be tz-aware")


@dataclass(frozen=True, slots=True)
class OrderResponse:
    """Broker response after place/cancel."""
    success: bool
    broker_order_id: str | None
    client_order_id: str
    status: OrderStatus
    error_code: str | None
    error_message: str | None
    received_at_utc: datetime


class BrokerExecutionInterface(ABC):
    """Write operations. **Task 40 ONLY** may call these.

    A KIS adapter implementing this interface is the only path to live orders.
    Task 9 scripts must NOT instantiate this — only Task 40 ExecutionGateway.

    Implementations MUST verify:
        1. self.mode == BrokerMode.PROD requires JCPR_ALLOW_LIVE=1 env var.
        2. Every place_order call has a non-empty approval_id from Task 40.
        3. client_order_id idempotency — same id → same outcome.
        4. ESC/Ctrl-C signal cancellation prevails over new orders.
    """

    @abstractmethod
    def place_order(self, request: OrderRequest) -> OrderResponse:
        """Place a new order. Idempotent on client_order_id."""
        ...

    @abstractmethod
    def cancel_order(
        self,
        *,
        broker_order_id: str,
        approval_id: str,
    ) -> OrderResponse:
        """Cancel an existing order. Requires Task 40 approval_id."""
        ...


__all__ = (
    "BrokerMode",
    "OrderSide",
    "OrderType",
    "OrderStatus",
    "AccountSummary",
    "Position",
    "Order",
    "OrderRequest",
    "OrderResponse",
    "ConnectionCheck",
    "BrokerAdapter",
    "BrokerExecutionInterface",
)
