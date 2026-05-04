"""브로커 어댑터 추상 베이스 (Abstract broker adapter).

다중 브로커 지원을 위한 인터페이스. 모든 구체 어댑터는 본 클래스를 상속한다.
- 첫 구현: KIS adapter (Task 8) — src/brokers/kis_adapter.py
- 추후 추가 가능: 키움, NH, 미래에셋 등

설계 원칙 (Design principles):
1. 브로커 중립적 (broker-neutral) — 어떤 KRX 브로커든 동일 인터페이스로 표현
2. 시크릿 미반환 — 어떤 메서드도 API key/token 자체를 반환하지 않음
3. 표준화된 예외 — 브로커별 오류는 errors.py 의 트리로 매핑
4. Idempotency 지원 — place_order 는 client_order_id 로 중복 방지 (Task 22)
5. 레이트 리밋 노출 — 상위 모듈이 백오프 전략을 짤 수 있도록

관련 모듈 (Related modules):
- src/brokers/types.py               — 공통 데이터 타입
- src/brokers/errors.py              — 표준화된 예외 계층
- src/execution/execution_gateway.py — Task 21, 본 인터페이스의 주 사용처
- src/execution/idempotency.py       — Task 22, client_order_id 발급
- src/risk/risk_gate.py              — Task 19, 주문 발주 전 게이트
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
from typing import Optional, Sequence

from .types import (
    Account,
    Fill,
    HealthStatus,
    OrderAck,
    OrderIntent,
    OrderStatus,
    Position,
    Quote,
    RateLimitInfo,
)


# ============================================================
# 추상 베이스 (Abstract Base)
# ============================================================
class BrokerAdapter(ABC):
    """모든 브로커 어댑터의 추상 베이스.

    하위 클래스 구현 시 주의사항 (Implementation notes):
    - __init__ 에서 인증 정보를 받되, 평문 보관하지 말고 세션 토큰으로 즉시 변환.
    - 토큰은 인스턴스 내부 비공개 속성(_token)으로만 보관, 메서드로 반환 금지.
    - 브로커별 raw 응답은 본 모듈의 표준 타입으로 변환 후 반환.
    - 브로커별 오류 코드는 errors.py 의 예외 트리로 매핑.
    - 모든 메서드는 호출 시점에 self-contained 하게 동작 (외부 상태 의존 최소화).
    """

    # ============================================================
    # 1. 메타 정보 (Identity)
    # ============================================================
    @property
    @abstractmethod
    def name(self) -> str:
        """브로커명 — 'kis', 'kiwoom', 'nh' 등 소문자 단일 토큰.

        로깅·라우팅·설정 매핑에 사용된다.
        """

    @property
    @abstractmethod
    def supports_paper(self) -> bool:
        """모의투자(paper) 계좌를 지원하는가?

        지원 시, OPERATING_MODE='paper' 환경변수로 페이퍼/실거래 분기 가능.
        """

    @abstractmethod
    def rate_limit_info(self) -> RateLimitInfo:
        """브로커의 레이트 리밋 사양 반환.

        상위 모듈은 본 정보로 호출 빈도를 조절한다.
        실제 한도와 다를 경우 보수적으로(낮게) 보고할 것을 권장.
        """

    # ============================================================
    # 2. 인증 / 세션 (Authentication & Session)
    # ============================================================
    @abstractmethod
    def authenticate(self) -> None:
        """토큰 발급 또는 갱신.

        SECURITY:
        - 본 메서드는 토큰 자체를 반환하지 않는다.
        - 토큰은 인스턴스 내부에 비공개 속성으로 저장.
        - 토큰을 로그·예외·반환값에 노출 금지.

        실패 시 errors.AuthError 또는 errors.PermissionError 를 던진다.
        """

    @abstractmethod
    def is_authenticated(self) -> bool:
        """현재 토큰이 유효한가? (만료/회전 필요 여부 판단용)

        네트워크 호출 없이 로컬 상태만으로 판단한다 (e.g. 토큰 만료 시각 비교).
        실제 유효성은 health_check() 로 확인.
        """

    @abstractmethod
    def health_check(self) -> HealthStatus:
        """브로커 연결 상태 점검.

        가벼운 read-only 호출로 연결성·인증을 검증한다 (예: 계좌 요약 1건 조회).
        """

    # ============================================================
    # 3. 계좌 / 포지션 / 잔고 (Account, Positions, Cash)
    # ============================================================
    @abstractmethod
    def get_account(self) -> Account:
        """계좌 요약 조회.

        SECURITY: 반환되는 Account.account_id_masked 는 반드시 마스킹된 형태.
        types.py 의 검증기가 '***' 미포함 시 모델 생성을 거부한다.
        """

    @abstractmethod
    def get_positions(self) -> Sequence[Position]:
        """현재 보유 포지션 목록.

        수량이 0인 포지션은 반환하지 않는다.
        """

    @abstractmethod
    def get_cash_balance(self) -> Decimal:
        """가용 현금 잔고 (account.currency 기준)."""

    # ============================================================
    # 4. 시세 (Quotes)
    # ============================================================
    @abstractmethod
    def get_quote(self, symbol: str) -> Quote:
        """단일 심볼 시세 스냅샷.

        Raises:
            errors.NotFoundError: 미존재 심볼.
            errors.ValidationError: 잘못된 심볼 형식.
        """

    # ============================================================
    # 5. 주문 (Orders)
    # ============================================================
    @abstractmethod
    def place_order(self, intent: OrderIntent) -> OrderAck:
        """주문 발주.

        IDEMPOTENCY:
        - intent.client_order_id 로 중복 발주 방지 (Task 22).
        - 동일 client_order_id 로 두 번 호출 시:
          * 첫 호출의 OrderAck 를 그대로 반환하거나,
          * errors.ValidationError 로 거부 (어댑터 정책).

        EMERGENCY STOP:
        - 본 메서드 자체는 비상 정지 검사를 수행하지 않는다.
        - 비상 정지는 호출자(execution_gateway, risk_gate)가 책임진다.
        - 비상 정지가 활성이면 호출자가 본 메서드를 호출하지 않아야 한다.

        Raises:
            errors.OrderRejectedError: 브로커가 주문 거부.
            errors.MarketClosedError:  시장 마감.
            errors.AuthError:          토큰 만료 등.
            errors.RateLimitError:     레이트 리밋.
            errors.TransientError:     일시적 오류 (재시도 가능).
        """

    @abstractmethod
    def cancel_order(self, broker_order_id: str) -> OrderAck:
        """미체결 주문 취소.

        취소 후 OrderAck 의 status 는 CANCELLED 또는 부분 체결 후 잔량 취소
        상황을 반영한다.

        Raises:
            errors.NotFoundError: 미존재 주문 ID.
        """

    @abstractmethod
    def get_order_status(self, broker_order_id: str) -> OrderStatus:
        """단일 주문의 현재 상태."""

    @abstractmethod
    def get_fills(self, since: datetime) -> Sequence[Fill]:
        """since 이후 발생한 체결 목록.

        Task 24 (fills ingestion) 의 입력으로 사용.
        시간 비교는 브로커의 ts 기준이며, 포함 경계는 '>=since'.
        """

    # ============================================================
    # 6. 시장 캘린더 (Market Calendar)
    # ============================================================
    @abstractmethod
    def is_market_open(self, ts: Optional[datetime] = None) -> bool:
        """주어진 시각(또는 현재)에 시장이 열려 있는가?

        Note:
        - 어댑터가 자체 캘린더를 제공하지 않으면 NotImplementedError 를 던진다.
        - 그 경우 src/data/calendar.py (Task 11) 의 캘린더가 사용된다.
        """

    # ============================================================
    # 7. 표현 (Representation)
    # ============================================================
    def __repr__(self) -> str:
        """디버깅용 repr — 시크릿이 들어가지 않도록 name 만 노출."""
        return f"<{type(self).__name__} name={self.name!r}>"
