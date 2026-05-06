"""
KIS 어댑터 Facade (KIS Adapter Facade)
========================================

JCPR Trading System - jcpr-ts-v01
Task 8 v0.1

모든 KIS 모듈의 단일 진입점.
(Single entry point for all KIS modules.)

사용 예 (Usage):
    adapter = KISAdapter.from_env(env_file=".env")
    
    # 시세 (always live)
    bars = adapter.market_data.fetch_bars("005930", Timeframe.D1, start, end)
    snap = adapter.quote.snapshot("005930")
    
    # 계좌
    account = adapter.account.fetch_account_snapshot()
    
    # 주문 (기본 dry-run)
    response = adapter.orders.submit_order(req)  # is_dry_run=True
    
    # 실거래 활성화 (Task 21+ 통합 후)
    adapter.orders.guard.enable_live(reason="Task 21 integration verified")
    response = adapter.orders.submit_order(req)  # is_dry_run=False
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from .account import KISAccount
from .auth import KISAuth
from .client import KISClient
from .credentials import KISCredentials, KISEnv, load_kis_credentials_from_env
from .market_data import KISMarketDataSource
from .orders import KISOrderClient, OrdersDryRunGuard
from .quote import KISQuoteSource

logger = logging.getLogger(__name__)


class KISAdapter:
    """
    KIS OpenAPI 통합 어댑터.

    Components:
        - market_data (Task 12 호환)
        - quote (Task 13 호환)
        - account
        - orders (기본 dry-run)
    """

    def __init__(
        self,
        credentials: KISCredentials,
        *,
        http_session=None,
    ):
        self._creds = credentials
        self._auth = KISAuth(credentials, http_session=http_session)
        self._client = KISClient(self._auth, http_session=http_session)
        self._dry_run_guard = OrdersDryRunGuard()  # 기본 dry-run

        # 컴포넌트 초기화
        self._market_data = KISMarketDataSource(self._client)
        self._quote = KISQuoteSource(self._client)
        self._account = KISAccount(self._client)
        self._orders = KISOrderClient(self._client, self._dry_run_guard)

        logger.info(
            "KIS 어댑터 초기화 완료: env=%s, dry_run=True (default)",
            credentials.env.value,
        )

    @classmethod
    def from_env(
        cls,
        env_file: Optional[str | Path] = None,
        *,
        override_env: Optional[KISEnv] = None,
        http_session=None,
    ) -> "KISAdapter":
        """
        .env 파일 또는 OS 환경변수에서 자격증명 로드 후 어댑터 생성.

        Args:
            env_file: .env 파일 경로 (선택). None이면 OS 환경변수만.
            override_env: KIS_ENV 환경변수를 무시하고 강제 설정 (paper/live 명시 전환)
            http_session: requests.Session (테스트 주입)
        """
        creds = load_kis_credentials_from_env(env_file, override_env=override_env)
        return cls(creds, http_session=http_session)

    # ---------- 컴포넌트 접근자 ----------

    @property
    def credentials(self) -> KISCredentials:
        return self._creds

    @property
    def env(self) -> KISEnv:
        return self._creds.env

    @property
    def auth(self) -> KISAuth:
        return self._auth

    @property
    def client(self) -> KISClient:
        return self._client

    @property
    def market_data(self) -> KISMarketDataSource:
        return self._market_data

    @property
    def quote(self) -> KISQuoteSource:
        return self._quote

    @property
    def account(self) -> KISAccount:
        return self._account

    @property
    def orders(self) -> KISOrderClient:
        return self._orders

    @property
    def dry_run_guard(self) -> OrdersDryRunGuard:
        """편의 접근자 — orders.guard와 동일."""
        return self._dry_run_guard

    # ---------- 안전 표시 ----------

    def __repr__(self) -> str:
        return (
            f"KISAdapter(env={self.env.value}, "
            f"creds={self._creds!r}, "
            f"orders_live_enabled={self._dry_run_guard.live_enabled})"
        )
