"""
KIS HTTP 클라이언트 (KIS HTTP Client)
=====================================

JCPR Trading System - jcpr-ts-v01
Task 8 v0.1

Rate limit + 재시도 + 에러 처리 통합 HTTP 클라이언트.
(Integrated HTTP client with rate limiting, retry, and error handling.)

원칙 (Principles):
- 토큰버킷 rate limit (KIS 한도 보수적 준수)
- 재시도: 5xx, 401(token expired) — 최대 1회, 백오프
- 응답 검증: KIS rt_cd != "0" → 예외
- 비밀 값은 로그에 노출 안 함 (헤더 마스킹)
- fail-closed: 모든 실패는 예외로 호출자에 전파
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from typing import Any, Optional

from .auth import AuthError, KISAuth

logger = logging.getLogger(__name__)


class KISAPIError(RuntimeError):
    """KIS API 오류 (rt_cd != '0' 등)."""

    def __init__(
        self,
        message: str,
        *,
        rt_cd: Optional[str] = None,
        msg_cd: Optional[str] = None,
        msg: Optional[str] = None,
        http_status: Optional[int] = None,
    ):
        super().__init__(message)
        self.rt_cd = rt_cd
        self.msg_cd = msg_cd
        self.msg = msg
        self.http_status = http_status


class RateLimitError(RuntimeError):
    """Rate limit 초과 — 호출자가 대기 후 재시도해야 함."""


# ─────────────────────────────────────────────────
# 토큰버킷 Rate Limiter
# ─────────────────────────────────────────────────

class _TokenBucket:
    """
    초당 N건 제한.
    (N requests per second.)

    슬라이딩 윈도우 — 최근 1초 이내 요청 수가 N 미만이어야 통과.
    """

    def __init__(self, rate_per_sec: int):
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec 양수 필요")
        self._rate = rate_per_sec
        self._timestamps: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self, *, max_wait_sec: float = 5.0) -> None:
        """
        요청 슬롯 점유. 한도 도달 시 대기 (또는 RateLimitError).
        """
        deadline = time.monotonic() + max_wait_sec
        while True:
            with self._lock:
                now = time.monotonic()
                # 1초 이전 기록 제거
                while self._timestamps and self._timestamps[0] <= now - 1.0:
                    self._timestamps.popleft()
                if len(self._timestamps) < self._rate:
                    self._timestamps.append(now)
                    return
                # 가장 오래된 기록이 1초 경과해야 슬롯 발생
                wait_until = self._timestamps[0] + 1.0
            if time.monotonic() >= deadline:
                raise RateLimitError(
                    f"Rate limit 대기 시간 초과: {max_wait_sec}s, rate={self._rate}/s"
                )
            time.sleep(min(wait_until - time.monotonic(), 0.05))

    def current_usage(self) -> int:
        """현재 1초 윈도우 내 사용량 (디버그용)."""
        with self._lock:
            now = time.monotonic()
            while self._timestamps and self._timestamps[0] <= now - 1.0:
                self._timestamps.popleft()
            return len(self._timestamps)


# ─────────────────────────────────────────────────
# KIS HTTP 클라이언트
# ─────────────────────────────────────────────────

class KISClient:
    """
    KIS REST API HTTP 클라이언트.
    (KIS REST API HTTP client.)
    """

    # 재시도 가능한 HTTP 상태
    _RETRYABLE_STATUSES = {500, 502, 503, 504}
    # 토큰 만료 의심 → 한 번 재발급 시도
    _AUTH_REFRESH_STATUSES = {401, 403}

    def __init__(self, auth: KISAuth, *, http_session=None):
        self._auth = auth
        self._creds = auth.credentials
        self._bucket = _TokenBucket(self._creds.rate_limit_per_sec)
        self._session = http_session  # None이면 lazy-init

    @property
    def base_url(self) -> str:
        return self._creds.env.base_url()

    @property
    def rate_limiter(self) -> _TokenBucket:
        return self._bucket

    # ------------------------------------------------------------------
    # 메인 호출 메서드
    # ------------------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        tr_id: str,
        params: Optional[dict[str, Any]] = None,
        body: Optional[dict[str, Any]] = None,
        custtype: str = "P",          # P=개인, B=법인
        max_retries: int = 1,
    ) -> dict[str, Any]:
        """
        KIS API 호출.

        Args:
            method: "GET" | "POST"
            path: e.g., "/uapi/domestic-stock/v1/trading/order-cash"
            tr_id: KIS TR ID (tr_codes.get_tr_code 사용)
            params: GET 쿼리 / POST는 사용 안 함
            body: POST body
            custtype: 고객유형
            max_retries: 재시도 횟수

        Returns:
            응답 dict (rt_cd == "0"인 경우만)

        Raises:
            KISAPIError, RateLimitError, AuthError
        """
        method = method.upper()
        if method not in ("GET", "POST"):
            raise ValueError(f"지원 안 하는 메서드: {method}")

        url = self.base_url + path

        # Rate limit 통과
        self._bucket.acquire()

        # 토큰 (자동 갱신)
        token = self._auth.get_token()

        headers = self._build_headers(token=token.token, tr_id=tr_id, custtype=custtype)

        # HTTP 호출 + 재시도
        attempts = 0
        last_error: Optional[Exception] = None
        token_already_refreshed = False

        while attempts <= max_retries:
            attempts += 1
            try:
                resp = self._http_call(method, url, headers=headers, params=params, body=body)
            except Exception as e:  # noqa: BLE001 - 네트워크 오류 일반 처리
                last_error = e
                if attempts > max_retries:
                    raise KISAPIError(
                        f"KIS HTTP 네트워크 오류 ({type(e).__name__}): {e}"
                    ) from e
                self._sleep_backoff(attempts)
                continue

            # HTTP 상태 처리
            if resp.status_code == 200:
                return self._parse_response(resp, tr_id=tr_id)

            # 토큰 만료 의심 → 1회 재발급
            if (
                resp.status_code in self._AUTH_REFRESH_STATUSES
                and not token_already_refreshed
            ):
                logger.info("KIS 401/403 → 토큰 강제 재발급")
                self._auth.invalidate()
                token = self._auth.get_token(force_refresh=True)
                headers = self._build_headers(
                    token=token.token, tr_id=tr_id, custtype=custtype,
                )
                token_already_refreshed = True
                continue

            # 5xx 재시도
            if resp.status_code in self._RETRYABLE_STATUSES and attempts <= max_retries:
                self._sleep_backoff(attempts)
                continue

            # 그 외는 즉시 실패
            self._raise_kis_error(resp, tr_id=tr_id)

        # while 종료 — 마지막 에러
        raise KISAPIError(
            f"KIS 호출 모든 재시도 실패: tr_id={tr_id}, last={last_error}"
        )

    # ------------------------------------------------------------------
    # 내부 헬퍼
    # ------------------------------------------------------------------

    def _build_headers(self, *, token: str, tr_id: str, custtype: str) -> dict[str, str]:
        return {
            "Content-Type": "application/json; charset=utf-8",
            "authorization": f"Bearer {token}",
            "appkey": self._creds.app_key,
            "appsecret": self._creds.app_secret,
            "tr_id": tr_id,
            "custtype": custtype,
        }

    def _http_call(self, method: str, url: str, *, headers, params, body):
        if self._session is None:
            try:
                import requests
            except ImportError as e:
                raise KISAPIError(
                    "'requests' 라이브러리 필요 — pip install requests"
                ) from e
            session = requests.Session()
        else:
            session = self._session

        if method == "GET":
            return session.get(
                url, headers=headers, params=params,
                timeout=self._creds.request_timeout_sec,
            )
        return session.post(
            url, headers=headers,
            data=json.dumps(body) if body else None,
            timeout=self._creds.request_timeout_sec,
        )

    @staticmethod
    def _sleep_backoff(attempt: int) -> None:
        # 0.5s, 1s, 2s
        delay = 0.5 * (2 ** (attempt - 1))
        time.sleep(min(delay, 2.0))

    @staticmethod
    def _parse_response(resp, *, tr_id: str) -> dict[str, Any]:
        try:
            data = resp.json()
        except Exception as e:
            raise KISAPIError(
                f"KIS 응답 JSON 파싱 실패 (tr_id={tr_id}): {e}",
                http_status=resp.status_code,
            ) from e

        # KIS 표준 응답: rt_cd가 "0"이어야 성공
        rt_cd = data.get("rt_cd")
        if rt_cd != "0":
            raise KISAPIError(
                f"KIS API 오류 (tr_id={tr_id}): rt_cd={rt_cd}, "
                f"msg_cd={data.get('msg_cd')}, msg={data.get('msg1', '')}",
                rt_cd=rt_cd,
                msg_cd=data.get("msg_cd"),
                msg=data.get("msg1"),
                http_status=resp.status_code,
            )
        return data

    @staticmethod
    def _raise_kis_error(resp, *, tr_id: str) -> None:
        # 본문에 비밀 노출 없이 status만
        body_preview = ""
        try:
            data = resp.json()
            body_preview = (
                f" rt_cd={data.get('rt_cd')} msg={data.get('msg1', '')[:80]}"
            )
        except Exception:
            pass
        raise KISAPIError(
            f"KIS HTTP {resp.status_code} (tr_id={tr_id}){body_preview}",
            http_status=resp.status_code,
        )
