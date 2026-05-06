"""
KIS 인증 (KIS Authentication)
==============================

JCPR Trading System - jcpr-ts-v01
Task 8 v0.1

KIS OAuth2 액세스 토큰 발급/갱신/캐시.
(KIS OAuth2 access token issuance, refresh, and caching.)

원칙 (Principles):
- 토큰은 메모리에만 보관 (절대 디스크/Git 저장 안 함)
- TTL 만료 5분 전 자동 갱신
- 재발급 실패 시 fail-closed (예외)
- 동시 갱신 방지 (스레드 락)
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from .credentials import KISCredentials

logger = logging.getLogger(__name__)


class AuthError(RuntimeError):
    """인증 오류 (token issuance/refresh 실패)."""


# 토큰 만료 5분 전에 갱신
TOKEN_REFRESH_MARGIN = timedelta(minutes=5)


@dataclass(frozen=True)
class AccessToken:
    """
    KIS 액세스 토큰 (메모리 전용).
    (Access token — memory only.)
    """
    token: str
    token_type: str           # 통상 "Bearer"
    expires_at_utc: datetime  # 절대 만료 시각

    def is_expired(self, now_utc: datetime, margin: timedelta = TOKEN_REFRESH_MARGIN) -> bool:
        return now_utc + margin >= self.expires_at_utc

    # 보안: 토큰 자체는 출력하지 않음
    def __repr__(self) -> str:
        return (
            f"AccessToken(token=****{self.token[-4:] if len(self.token) >= 4 else '****'}, "
            f"type={self.token_type}, expires_at={self.expires_at_utc.isoformat()})"
        )

    def __str__(self) -> str:
        return self.__repr__()


class KISAuth:
    """
    KIS 인증 매니저 — 토큰 발급/갱신/캐시.

    실제 HTTP 호출은 KISClient에서 수행, 이 클래스는 토큰 라이프사이클만 관리.
    """

    OAUTH_PATH = "/oauth2/tokenP"  # KIS 토큰 발급 엔드포인트

    def __init__(self, credentials: KISCredentials, http_session=None):
        """
        Args:
            credentials: KIS 자격증명
            http_session: requests.Session (테스트 주입 가능)
        """
        self._creds = credentials
        self._session = http_session  # None이면 lazy-init
        self._token: Optional[AccessToken] = None
        self._lock = threading.Lock()

    @property
    def credentials(self) -> KISCredentials:
        return self._creds

    def get_token(self, *, force_refresh: bool = False) -> AccessToken:
        """
        유효한 토큰 반환. 만료 시 자동 갱신.
        (Return a valid token — auto-refresh if expired.)

        Raises:
            AuthError: 토큰 발급 실패
        """
        now = datetime.now(timezone.utc)

        with self._lock:
            if (
                not force_refresh
                and self._token is not None
                and not self._token.is_expired(now)
            ):
                return self._token

            new_token = self._issue_new_token()
            self._token = new_token
            logger.info(
                "KIS 토큰 발급 (token issued): expires_at=%s env=%s",
                new_token.expires_at_utc.isoformat(),
                self._creds.env.value,
            )
            return new_token

    def invalidate(self) -> None:
        """캐시된 토큰 무효화 (401 응답 후 강제 재발급용)."""
        with self._lock:
            self._token = None

    # ------------------------------------------------------------------
    # 토큰 발급 — HTTP 호출
    # ------------------------------------------------------------------

    def _issue_new_token(self) -> AccessToken:
        """
        KIS /oauth2/tokenP 호출.
        Body: {"grant_type":"client_credentials", "appkey":..., "appsecret":...}
        Response: {"access_token":"...", "token_type":"Bearer", "expires_in":86400}
        """
        url = self._creds.env.base_url() + self.OAUTH_PATH
        body = {
            "grant_type": "client_credentials",
            "appkey": self._creds.app_key,
            "appsecret": self._creds.app_secret,
        }
        headers = {"Content-Type": "application/json; charset=utf-8"}

        # lazy import — requests는 선택적 의존성
        if self._session is None:
            try:
                import requests
            except ImportError as e:
                raise AuthError(
                    "'requests' 라이브러리 필요 — pip install requests"
                ) from e
            session = requests.Session()
        else:
            session = self._session

        try:
            resp = session.post(
                url,
                data=json.dumps(body),
                headers=headers,
                timeout=self._creds.request_timeout_sec,
            )
        except Exception as e:  # noqa: BLE001 - network errors all fail-closed
            raise AuthError(f"KIS 토큰 발급 네트워크 오류: {type(e).__name__}: {e}") from e

        if resp.status_code != 200:
            # 응답 본문에 비밀이 노출되지 않도록 status만 보고
            raise AuthError(
                f"KIS 토큰 발급 실패 (HTTP {resp.status_code}) — "
                f"env={self._creds.env.value}"
            )

        try:
            data = resp.json()
        except Exception as e:
            raise AuthError(f"KIS 토큰 응답 JSON 파싱 실패: {e}") from e

        token_str = data.get("access_token")
        token_type = data.get("token_type", "Bearer")
        expires_in = data.get("expires_in")

        if not token_str:
            raise AuthError("KIS 응답에 access_token 누락")
        if not isinstance(expires_in, int) or expires_in <= 0:
            raise AuthError(f"KIS 응답 expires_in 부적절: {expires_in!r}")

        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        return AccessToken(
            token=token_str,
            token_type=token_type,
            expires_at_utc=expires_at,
        )
