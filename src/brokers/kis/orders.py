"""
KIS 주문 모듈 (KIS Order Module)
=================================

JCPR Trading System - jcpr-ts-v01
Task 8 v0.1

⚠️ 안전 가드 (Safety Guard):
이 모듈은 주문 송수신 코드를 완성된 형태로 제공하지만,
**실제 주문 송신은 OrdersDryRunGuard.live_enabled = True 일 때만** 수행됩니다.
(Live order submission only when explicitly enabled.)

Task 21 (실행 게이트웨이) + Task 40 (인간 승인 워크플로우) 통합 후
명시적으로 활성화하기 전까지는 dry-run 모드 — 실제 송신 없이 응답 시뮬레이션.

원칙:
- fail-closed: live_enabled=False면 실 주문 절대 송신 안 함
- 멱등 client_order_id 지원 (Task 22)
- 주문 송신 전 capacity/risk gate 통과 가정 (호출자 책임)
- 모든 주문 audit 로그 (TODO Task 21)
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Literal, Optional

from .client import KISClient, KISAPIError
from .tr_codes import get_tr_code

logger = logging.getLogger(__name__)


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    """주문 구분 — KIS ORD_DVSN 매핑."""
    LIMIT = "limit"        # 지정가 (00)
    MARKET = "market"      # 시장가 (01)


# KIS ORD_DVSN 코드 매핑
_ORD_DVSN_MAP = {
    OrderType.LIMIT: "00",
    OrderType.MARKET: "01",
}


@dataclass(frozen=True)
class OrderRequest:
    """주문 요청 — Task 17 OrderIntent에서 변환."""
    symbol: str
    side: OrderSide
    quantity: int
    order_type: OrderType
    limit_price: Optional[Decimal] = None  # MARKET 주문은 None
    client_order_id: Optional[str] = None  # 멱등 키 (없으면 자동 생성)

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError(f"quantity 양수 필요: {self.quantity}")
        if self.order_type is OrderType.LIMIT:
            if self.limit_price is None or self.limit_price <= 0:
                raise ValueError("LIMIT 주문은 limit_price 필요")
        if not self.symbol or len(self.symbol) != 6:
            raise ValueError(f"잘못된 KRX 코드: {self.symbol!r}")


@dataclass(frozen=True)
class OrderResponse:
    """주문 응답."""
    accepted: bool
    client_order_id: str
    broker_order_no: Optional[str]   # KIS ODNO (주문번호)
    submitted_at_utc: datetime
    is_dry_run: bool                 # True면 실제 송신 안 됨
    raw_response: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


# ─────────────────────────────────────────────────
# Dry-Run Guard — 실 주문 송신 활성화 게이트
# ─────────────────────────────────────────────────

class OrdersDryRunGuard:
    """
    주문 모듈의 실제 송신 활성화 게이트.
    (Activation gate for real order submission.)

    기본은 live_enabled=False — 모든 주문이 dry-run으로 시뮬레이션.
    Task 21 + Task 40 통합 후 명시적 .enable_live() 호출로 활성화.

    이 객체는 KISOrderClient에 주입되어 모든 주문 송신 시 검증됨.
    """

    def __init__(self):
        self._live_enabled = False
        self._enabled_reason: Optional[str] = None
        self._enabled_at_utc: Optional[datetime] = None
        self._lock = threading.Lock()

    @property
    def live_enabled(self) -> bool:
        with self._lock:
            return self._live_enabled

    def enable_live(self, reason: str) -> None:
        """
        실제 주문 송신 활성화.
        (Enable live order submission.)

        Args:
            reason: 활성화 사유 (audit log용 — 절대 빈 값 안 됨)

        Raises:
            ValueError: reason이 비어있으면 거부
        """
        if not reason or not reason.strip():
            raise ValueError("실거래 활성화에는 명시적 사유 필요 (no empty reason)")
        with self._lock:
            self._live_enabled = True
            self._enabled_reason = reason.strip()
            self._enabled_at_utc = datetime.now(timezone.utc)
        logger.warning(
            "🔥 KIS Orders LIVE 활성화 (orders live enabled): reason=%r at=%s",
            reason, self._enabled_at_utc.isoformat(),
        )

    def disable_live(self) -> None:
        """실거래 비활성화 (안전 복귀)."""
        with self._lock:
            self._live_enabled = False
            self._enabled_reason = None
        logger.info("KIS Orders LIVE 비활성화 (back to dry-run)")

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "live_enabled": self._live_enabled,
                "enabled_reason": self._enabled_reason,
                "enabled_at_utc": (
                    self._enabled_at_utc.isoformat()
                    if self._enabled_at_utc else None
                ),
            }


# ─────────────────────────────────────────────────
# KIS Order Client
# ─────────────────────────────────────────────────

class KISOrderClient:
    """
    KIS 주문 송수신 클라이언트.
    (KIS order client.)
    """

    ORDER_PATH = "/uapi/domestic-stock/v1/trading/order-cash"

    def __init__(self, client: KISClient, dry_run_guard: OrdersDryRunGuard):
        self._client = client
        self._creds = client._creds
        self._env = client._creds.env
        self._guard = dry_run_guard

    @property
    def guard(self) -> OrdersDryRunGuard:
        return self._guard

    # ------------------------------------------------------------------
    # 주문 송신
    # ------------------------------------------------------------------

    def submit_order(self, req: OrderRequest) -> OrderResponse:
        """
        주문 송신.

        live_enabled=False (기본): dry-run — 실제 KIS API 호출 안 함, 시뮬 응답
        live_enabled=True: 실제 KIS API 호출

        Returns:
            OrderResponse — accepted/실패 여부 + broker_order_no
        """
        client_order_id = req.client_order_id or self._generate_client_order_id()
        now = datetime.now(timezone.utc)

        # ─── Dry-Run 모드 ───
        if not self._guard.live_enabled:
            logger.info(
                "DRY-RUN 주문 (dry-run order): symbol=%s side=%s qty=%d type=%s price=%s coid=%s",
                req.symbol, req.side.value, req.quantity, req.order_type.value,
                req.limit_price, client_order_id,
            )
            return OrderResponse(
                accepted=True,
                client_order_id=client_order_id,
                broker_order_no=None,
                submitted_at_utc=now,
                is_dry_run=True,
                raw_response={"note": "dry-run — no actual order submitted"},
            )

        # ─── LIVE 모드 — 실제 송신 ───
        return self._submit_live(req, client_order_id, now)

    def _submit_live(
        self, req: OrderRequest, client_order_id: str, now: datetime,
    ) -> OrderResponse:
        """실제 KIS API 호출 — live_enabled=True일 때만 도달."""
        function = "order_buy" if req.side is OrderSide.BUY else "order_sell"
        tr_id = get_tr_code(function, self._env)
        ord_dvsn = _ORD_DVSN_MAP[req.order_type]

        # 가격 — 시장가는 "0" 문자열로 전송
        if req.order_type is OrderType.MARKET:
            ord_unpr = "0"
        else:
            ord_unpr = str(int(req.limit_price))  # KIS는 정수 가격

        body = {
            "CANO": self._creds.account_cano,
            "ACNT_PRDT_CD": self._creds.account_prdt,
            "PDNO": req.symbol,
            "ORD_DVSN": ord_dvsn,
            "ORD_QTY": str(req.quantity),
            "ORD_UNPR": ord_unpr,
        }

        try:
            data = self._client.request(
                "POST", self.ORDER_PATH, tr_id=tr_id, body=body,
                # 주문은 재시도 안 함 (멱등성 보장 어려움 — 타임아웃은 호출자 처리)
                max_retries=0,
            )
        except KISAPIError as e:
            logger.error(
                "KIS 주문 실패 (live): coid=%s symbol=%s error=%s",
                client_order_id, req.symbol, e,
            )
            return OrderResponse(
                accepted=False,
                client_order_id=client_order_id,
                broker_order_no=None,
                submitted_at_utc=now,
                is_dry_run=False,
                raw_response={},
                error=str(e),
            )

        # 응답 파싱
        output = data.get("output") or {}
        broker_order_no = output.get("ODNO") or output.get("KRX_FWDG_ORD_ORGNO")

        logger.info(
            "KIS 주문 수락 (live): coid=%s symbol=%s broker_order_no=%s",
            client_order_id, req.symbol, broker_order_no,
        )

        return OrderResponse(
            accepted=True,
            client_order_id=client_order_id,
            broker_order_no=str(broker_order_no) if broker_order_no else None,
            submitted_at_utc=now,
            is_dry_run=False,
            raw_response=output,
        )

    # ------------------------------------------------------------------
    # 미체결 주문 조회
    # ------------------------------------------------------------------

    OPEN_ORDERS_PATH = "/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl"

    def fetch_open_orders(self) -> list[dict[str, Any]]:
        """
        미체결 주문 목록 조회.
        (Inquire pending/open orders.)

        Task 19 RiskContext.pending_orders 형식 호환:
        [{"symbol": ..., "side": ..., "status": ..., "order_id": ...}, ...]
        """
        if not self._guard.live_enabled:
            logger.debug("DRY-RUN — fetch_open_orders 빈 리스트 반환")
            return []

        tr_id = get_tr_code("open_orders", self._env)
        params = {
            "CANO": self._creds.account_cano,
            "ACNT_PRDT_CD": self._creds.account_prdt,
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
            "INQR_DVSN_1": "0",
            "INQR_DVSN_2": "0",
        }
        data = self._client.request(
            "GET", self.OPEN_ORDERS_PATH, tr_id=tr_id, params=params,
        )

        rows = data.get("output") or []
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                # KIS 매수/매도 구분: sll_buy_dvsn_cd "01"=매도, "02"=매수
                sll_buy = row.get("sll_buy_dvsn_cd")
                side = "sell" if sll_buy == "01" else "buy" if sll_buy == "02" else "unknown"

                result.append({
                    "order_id": row.get("odno"),
                    "symbol": row.get("pdno"),
                    "side": side,
                    "quantity": int(row.get("ord_qty") or 0),
                    "price": str(row.get("ord_unpr") or "0"),
                    "status": "pending",
                    "raw": row,
                })
            except (ValueError, TypeError, KeyError) as e:
                logger.warning("미체결 주문 행 파싱 실패: %s", e)
                continue
        return result

    # ------------------------------------------------------------------
    # 헬퍼
    # ------------------------------------------------------------------

    @staticmethod
    def _generate_client_order_id() -> str:
        """멱등 client_order_id (UUID4)."""
        return f"jcpr-{uuid.uuid4().hex[:16]}"
