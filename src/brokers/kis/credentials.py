"""
KIS 자격증명 (KIS Credentials)
==============================

JCPR Trading System - jcpr-ts-v01
Task 8 v0.1

.env 파일에서 KIS API 자격증명을 로드.
(Loads KIS API credentials from .env file.)

⚠️ 절대 원칙 (Absolute Principles):
- 비밀 값은 어떤 경우에도 로그/예외 메시지/Git에 노출 금지
- __repr__, __str__ 오버라이드로 마스킹 강제
- env 파일 누락 시 즉시 fail-closed (애매한 None 반환 안 함)
- 모의투자(paper)와 실거래(live) 명시적 전환

Zone: C (Local Only) — 이 모듈이 읽는 .env 파일은 절대 Git에 추적되지 않음.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


class KISEnv(str, Enum):
    """KIS 환경 구분."""
    PAPER = "paper"  # 모의투자 — 실자금 영향 없음
    LIVE = "live"    # 실거래 — 실자금 거래

    def base_url(self) -> str:
        """환경별 KIS REST API base URL."""
        if self is KISEnv.LIVE:
            return "https://openapi.koreainvestment.com:9443"
        return "https://openapivts.koreainvestment.com:29443"


class CredentialsError(RuntimeError):
    """자격증명 로드/검증 오류."""


def _mask(value: Optional[str], head: int = 3, tail: int = 2) -> str:
    """비밀 값을 마스킹. 'abc***xy' 형태."""
    if not value:
        return "<missing>"
    if len(value) <= head + tail:
        return "*" * len(value)
    return f"{value[:head]}***{value[-tail:]}"


@dataclass(frozen=True)
class KISCredentials:
    """
    KIS OpenAPI 자격증명.
    (KIS OpenAPI credentials.)

    이 객체는 절대 직렬화/로깅되지 않습니다.
    (This object must NEVER be serialized or logged in raw form.)
    """
    env: KISEnv
    app_key: str
    app_secret: str
    account_no: str           # "12345678-01" 형식
    hts_id: Optional[str] = None
    request_timeout_sec: int = 10
    rate_limit_per_sec: int = 15

    def __post_init__(self) -> None:
        if not self.app_key or not self.app_key.strip():
            raise CredentialsError("KIS_APP_KEY 누락 또는 공백")
        if not self.app_secret or not self.app_secret.strip():
            raise CredentialsError("KIS_APP_SECRET 누락 또는 공백")
        if not self.account_no or not self.account_no.strip():
            raise CredentialsError("KIS_ACCOUNT_NO 누락 또는 공백")
        # 계좌번호 형식 — 단순 검증 (X 8자리 - X 2자리)
        if "-" not in self.account_no:
            raise CredentialsError(
                "KIS_ACCOUNT_NO 형식 오류 (expected: '12345678-01')"
            )
        if self.request_timeout_sec <= 0:
            raise CredentialsError("request_timeout_sec 양수 필요")
        if self.rate_limit_per_sec <= 0 or self.rate_limit_per_sec > 20:
            raise CredentialsError(
                f"rate_limit_per_sec는 (0, 20] 범위 (KIS 한도 보수적): {self.rate_limit_per_sec}"
            )

    # ---------- 안전 표시 (Safe Display) — 비밀 노출 절대 금지 ----------

    def __repr__(self) -> str:
        return (
            f"KISCredentials(env={self.env.value}, "
            f"app_key={_mask(self.app_key)}, "
            f"app_secret=****, "
            f"account_no={_mask(self.account_no, 4, 2)}, "
            f"hts_id={_mask(self.hts_id) if self.hts_id else '<none>'})"
        )

    def __str__(self) -> str:
        return self.__repr__()

    @property
    def account_cano(self) -> str:
        """KIS API에서 사용하는 계좌번호 앞 8자리 (CANO)."""
        return self.account_no.split("-")[0]

    @property
    def account_prdt(self) -> str:
        """계좌번호 뒤 2자리 (계좌상품코드, ACNT_PRDT_CD)."""
        return self.account_no.split("-")[1]


# ─────────────────────────────────────────────────
# .env 로드 (Loader)
# ─────────────────────────────────────────────────

def load_kis_credentials_from_env(
    env_file: Optional[str | Path] = None,
    *,
    override_env: Optional[KISEnv] = None,
) -> KISCredentials:
    """
    .env 또는 OS 환경변수에서 KIS 자격증명 로드.
    (Load KIS credentials from .env or OS environment.)

    우선순위:
    1. env_file 인자 (지정 시) — .env 형식 파일
    2. OS 환경변수

    Args:
        env_file: .env 파일 경로 (선택)
        override_env: 환경변수 KIS_ENV를 무시하고 강제 전환 (테스트/명시적 전환용)

    Raises:
        CredentialsError: 필수 변수 누락
    """
    # .env 파일 로드 (있으면)
    if env_file is not None:
        _load_dotenv_simple(Path(env_file))

    # 환경변수 추출
    def _getenv(key: str, required: bool = True) -> Optional[str]:
        v = os.environ.get(key)
        if v is None or v.strip() == "":
            if required:
                raise CredentialsError(
                    f"환경변수 누락 (missing env var): {key} — "
                    f".env 파일 또는 OS 환경변수에 설정하세요"
                )
            return None
        return v.strip()

    # 환경 구분
    env_str = (_getenv("KIS_ENV", required=False) or "paper").lower()
    try:
        env = KISEnv(env_str)
    except ValueError:
        raise CredentialsError(
            f"잘못된 KIS_ENV 값: {env_str!r} (expected 'paper' or 'live')"
        )
    if override_env is not None:
        env = override_env

    return KISCredentials(
        env=env,
        app_key=_getenv("KIS_APP_KEY"),
        app_secret=_getenv("KIS_APP_SECRET"),
        account_no=_getenv("KIS_ACCOUNT_NO"),
        hts_id=_getenv("KIS_HTS_ID", required=False),
        request_timeout_sec=int(_getenv("KIS_REQUEST_TIMEOUT_SEC", required=False) or "10"),
        rate_limit_per_sec=int(_getenv("KIS_RATE_LIMIT_PER_SEC", required=False) or "15"),
    )


def _load_dotenv_simple(path: Path) -> None:
    """
    .env 파일을 OS 환경변수로 로드 (python-dotenv 의존성 없이).
    (Simple .env loader without python-dotenv dependency.)

    형식: KEY=VALUE (공백/따옴표 trim, # 주석 무시)
    """
    if not path.exists():
        raise CredentialsError(f".env 파일 없음 (not found): {path}")

    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            key = key.strip()
            value = value.strip()
            # 따옴표 trim
            if (value.startswith('"') and value.endswith('"')) or \
               (value.startswith("'") and value.endswith("'")):
                value = value[1:-1]
            # OS 환경변수에 이미 있으면 덮어쓰지 않음 (OS 우선)
            if key not in os.environ:
                os.environ[key] = value
