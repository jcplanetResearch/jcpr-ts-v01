"""
KIS OpenAPI 어댑터 (KIS OpenAPI Adapter)
=========================================

JCPR Trading System - jcpr-ts-v01
Task 8 v0.1

⚠️ 보안 경고 (Security Warning):
- KIS App Key/Secret은 절대 코드/Git에 포함하지 말 것
- 모든 자격증명은 .env 파일에서 로드 (Zone C — Local Only)
- KIS_ENV=paper (기본) → 모의투자 / KIS_ENV=live → 실거래 (주의!)

공개 API (Public API):
"""

from .credentials import KISCredentials, KISEnv
from .auth import KISAuth, AuthError
from .client import KISClient, KISAPIError, RateLimitError
from .market_data import KISMarketDataSource
from .quote import KISQuoteSource
from .account import KISAccount, AccountSnapshot, PositionInfo
from .orders import KISOrderClient, OrderRequest, OrderResponse, OrdersDryRunGuard
from .adapter import KISAdapter

__all__ = [
    "KISCredentials", "KISEnv",
    "KISAuth", "AuthError",
    "KISClient", "KISAPIError", "RateLimitError",
    "KISMarketDataSource",
    "KISQuoteSource",
    "KISAccount", "AccountSnapshot", "PositionInfo",
    "KISOrderClient", "OrderRequest", "OrderResponse", "OrdersDryRunGuard",
    "KISAdapter",
]
