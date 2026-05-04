"""표준화된 브로커 예외 계층 (Standardized broker exception hierarchy).

브로커별 오류 코드를 동일한 예외 트리로 변환하여, 상위 모듈
(risk_gate, execution_gateway 등)이 구체 어댑터 종류와 무관하게
처리할 수 있도록 한다.

설계 원칙 (Design principles):
- 모든 예외는 BrokerError 의 하위 클래스
- 시크릿(API key, token 등)은 예외 메시지·context 에 절대 포함 금지
- 재시도 가능 여부(retryable)를 클래스 단위로 명시
- 브로커 raw 응답은 sanitized form 으로만 보존

관련 모듈 (Related modules):
- src/brokers/base.py     — 본 예외를 던지는 추상 인터페이스
- src/brokers/types.py    — 공통 데이터 타입
- src/risk/risk_gate.py   — Task 19, 본 예외를 받아 거부 사유로 매핑
"""
from __future__ import annotations
from typing import Any, Mapping, Optional


# ============================================================
# 베이스 예외 (Base exception)
# ============================================================
class BrokerError(Exception):
    """모든 브로커 관련 예외의 베이스.

    Attributes:
        message: 사람이 읽을 수 있는 오류 메시지 (시크릿 미포함).
        broker_name: 어떤 어댑터에서 발생했는지 (e.g. "kis").
        broker_code: 브로커가 반환한 원본 코드 (선택, 마스킹 후 보관).
        context: 추가 컨텍스트 (시크릿 미포함, 자동 마스킹 권장).
        retryable: 동일 호출을 재시도해도 되는지 여부.
    """
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        broker_name: Optional[str] = None,
        broker_code: Optional[str] = None,
        context: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.broker_name = broker_name
        self.broker_code = broker_code
        self.context = dict(context) if context else {}

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"message={self.message!r}, "
            f"broker_name={self.broker_name!r}, "
            f"broker_code={self.broker_code!r}, "
            f"retryable={self.retryable})"
        )


# ============================================================
# 인증 / 권한 (Auth & Permission)
# ============================================================
class AuthError(BrokerError):
    """인증 실패 — 토큰 만료, 잘못된 자격증명 등 (HTTP 401 류)."""
    retryable = False


class PermissionError(BrokerError):  # noqa: A001 — 의도적 shadowing
    """권한 부족 — 계정에 해당 작업 권한 없음 (HTTP 403 류)."""
    retryable = False


# ============================================================
# 레이트 리밋 / 일시적 (Rate limit & Transient)
# ============================================================
class RateLimitError(BrokerError):
    """호출 빈도 한도 초과 (HTTP 429). 백오프 후 재시도 가능."""
    retryable = True

    def __init__(
        self,
        *args: Any,
        retry_after_seconds: Optional[float] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.retry_after_seconds = retry_after_seconds


class TransientError(BrokerError):
    """일시적 오류 — 5xx, 네트워크 타임아웃 등. 재시도 가능."""
    retryable = True


# ============================================================
# 주문 처리 (Order handling)
# ============================================================
class OrderRejectedError(BrokerError):
    """브로커가 주문 자체를 거부 — 잔고 부족, 호가 불가 등.

    재시도 무의미 (입력 자체를 다시 산출해야 함).
    """
    retryable = False


class NotFoundError(BrokerError):
    """요청한 자원이 존재하지 않음 — 주문 ID, 심볼 등."""
    retryable = False


class ValidationError(BrokerError):
    """입력값 자체가 잘못됨 — 잘못된 심볼 형식, 음수 수량 등."""
    retryable = False


# ============================================================
# 시장 상태 (Market state)
# ============================================================
class MarketClosedError(BrokerError):
    """시장이 마감되어 거래 불가."""
    retryable = False


# ============================================================
# 시크릿 누출 방지 헬퍼 (Secret-redaction helper)
# ============================================================
_SECRET_KEY_PATTERNS = (
    "api_key", "apikey", "api-secret", "api_secret",
    "token", "password", "authorization", "secret",
)


def redact_context(ctx: Mapping[str, Any]) -> dict[str, Any]:
    """컨텍스트 dict 에서 시크릿 의심 키를 마스킹.

    어댑터에서 BrokerError 를 던질 때, raw 응답·헤더를 그대로 넣지 말고
    이 함수를 통과시킨다.
    """
    redacted: dict[str, Any] = {}
    for k, v in ctx.items():
        lk = str(k).lower()
        if any(p in lk for p in _SECRET_KEY_PATTERNS):
            redacted[k] = "***REDACTED***"
        else:
            redacted[k] = v
    return redacted
