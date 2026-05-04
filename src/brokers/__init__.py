"""src/brokers — 브로커 어댑터 패키지.

다중 브로커 지원을 위한 추상 인터페이스 + 공통 타입 + 표준 예외.
"""
from .base import BrokerAdapter
from .types import (
    Side, OrderType, TimeInForce, OrderStatus,
    Quote, Position, Account,
    OrderIntent, OrderAck, Fill,
    HealthStatus, RateLimitInfo,
)
from .errors import (
    BrokerError,
    AuthError, PermissionError,
    RateLimitError, TransientError,
    OrderRejectedError, NotFoundError, ValidationError,
    MarketClosedError,
    redact_context,
)

__all__ = [
    # base
    "BrokerAdapter",
    # types
    "Side", "OrderType", "TimeInForce", "OrderStatus",
    "Quote", "Position", "Account",
    "OrderIntent", "OrderAck", "Fill",
    "HealthStatus", "RateLimitInfo",
    # errors
    "BrokerError",
    "AuthError", "PermissionError",
    "RateLimitError", "TransientError",
    "OrderRejectedError", "NotFoundError", "ValidationError",
    "MarketClosedError",
    "redact_context",
]
