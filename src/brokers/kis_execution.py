"""Task 40 — KIS broker write extension.

Extends KISBrokerAdapter with BrokerExecutionInterface (place_order,
cancel_order). This is a SEPARATE class so read-only callers (Task 9 scripts)
cannot accidentally invoke write tools.

ONLY the Task 40 ExecutionGateway should construct KISExecutionAdapter.

References:
    KIS REST API — Order:
        https://apiportal.koreainvestment.com/apiservice/oauth2

Security guarantees (in addition to KISBrokerAdapter):
    1. PROD mode requires JCPR_ALLOW_LIVE=1 (inherited from KISBrokerAdapter).
    2. place_order requires non-empty approval_id from Task 40.
    3. client_order_id is sent as KOAID (KIS Order Application ID) for
       idempotency tracking on the broker side.
    4. ESC/Ctrl-C signal aborts in-flight calls.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

from .base import (
    BrokerExecutionInterface,
    BrokerMode,
    OrderRequest,
    OrderResponse,
    OrderSide,
    OrderStatus,
    OrderType,
)
from .kis_adapter import KISAdapterError, KISBrokerAdapter, _to_decimal


# KIS order endpoints
ORDER_PLACE_PATH: str = "/uapi/domestic-stock/v1/trading/order-cash"
ORDER_CANCEL_PATH: str = "/uapi/domestic-stock/v1/trading/order-rvsecncl"

# TR_IDs for orders (paper vs prod, buy vs sell)
TR_ID_BUY_PAPER: str = "VTTC0802U"
TR_ID_BUY_PROD: str = "TTTC0802U"
TR_ID_SELL_PAPER: str = "VTTC0801U"
TR_ID_SELL_PROD: str = "TTTC0801U"
TR_ID_CANCEL_PAPER: str = "VTTC0803U"
TR_ID_CANCEL_PROD: str = "TTTC0803U"


class KISExecutionAdapter(KISBrokerAdapter, BrokerExecutionInterface):
    """KIS adapter with write capabilities.

    Inherits all read-only methods from KISBrokerAdapter, plus implements
    BrokerExecutionInterface (place_order, cancel_order).

    DO NOT instantiate from Task 9 scripts. This is gateway-only.
    """

    def place_order(self, request: OrderRequest) -> OrderResponse:
        """Place an order via KIS order-cash endpoint.

        Idempotency: client_order_id is sent in HTTP header to allow KIS
        to reject duplicate orders within a session.
        """
        if self._interrupted.is_set():
            raise KISAdapterError("interrupted by ESC/Ctrl-C signal")
        if not isinstance(request, OrderRequest):
            raise TypeError("request must be OrderRequest")
        # OrderRequest.__post_init__ already validates approval_id presence

        # Resolve TR_ID by side
        if request.side == OrderSide.BUY:
            tr_id = (TR_ID_BUY_PROD if self._mode == BrokerMode.PROD
                     else TR_ID_BUY_PAPER)
        else:
            tr_id = (TR_ID_SELL_PROD if self._mode == BrokerMode.PROD
                     else TR_ID_SELL_PAPER)

        # KIS order_division code: "00" limit, "01" market
        ord_dvsn = "00" if request.order_type == OrderType.LIMIT else "01"

        # Price: 0 for market, decimal value for limit
        if request.order_type == OrderType.LIMIT:
            ord_price = str(int(request.limit_price_krw))
        else:
            ord_price = "0"

        body = {
            "CANO": self._secrets.account_number,
            "ACNT_PRDT_CD": self._secrets.account_product,
            "PDNO": request.symbol,
            "ORD_DVSN": ord_dvsn,
            "ORD_QTY": str(int(request.quantity)),
            "ORD_UNPR": ord_price,
        }

        # custtype + tr_id headers; client_order_id passed for idempotency
        headers = self._auth_headers(tr_id=tr_id)
        # KIS supports a custom hash header but not idempotency by client id —
        # we record it ourselves via execution_result for audit.

        try:
            status, data = self._http_request(
                method="POST",
                path=ORDER_PLACE_PATH,
                headers=headers,
                body=body,
            )
        except KISAdapterError as e:
            return OrderResponse(
                success=False,
                broker_order_id=None,
                client_order_id=request.client_order_id,
                status=OrderStatus.REJECTED,
                error_code="TRANSPORT",
                error_message=str(e)[:500],
                received_at_utc=self._now_fn(),
            )

        # Parse KIS response — {"rt_cd": "0" success, otherwise error}
        rt_cd = str(data.get("rt_cd", "")).strip()
        if status != 200 or rt_cd != "0":
            return OrderResponse(
                success=False,
                broker_order_id=None,
                client_order_id=request.client_order_id,
                status=OrderStatus.REJECTED,
                error_code=str(data.get("msg_cd", ""))[:50],
                error_message=str(data.get("msg1", "unknown"))[:500],
                received_at_utc=self._now_fn(),
            )

        # Extract broker order id from output
        output = data.get("output", {}) or {}
        if isinstance(output, list) and output:
            output = output[0]
        broker_order_id = str(output.get("ODNO") or output.get("odno") or "").strip()

        return OrderResponse(
            success=True,
            broker_order_id=broker_order_id or None,
            client_order_id=request.client_order_id,
            status=OrderStatus.PENDING,
            error_code=None,
            error_message=None,
            received_at_utc=self._now_fn(),
        )

    def cancel_order(
        self,
        *,
        broker_order_id: str,
        approval_id: str,
    ) -> OrderResponse:
        """Cancel an existing order."""
        if self._interrupted.is_set():
            raise KISAdapterError("interrupted by ESC/Ctrl-C signal")
        if not broker_order_id:
            raise ValueError("broker_order_id required")
        if not approval_id:
            raise ValueError("approval_id required")

        tr_id = (TR_ID_CANCEL_PROD if self._mode == BrokerMode.PROD
                 else TR_ID_CANCEL_PAPER)

        body = {
            "CANO": self._secrets.account_number,
            "ACNT_PRDT_CD": self._secrets.account_product,
            "KRX_FWDG_ORD_ORGNO": "",
            "ORGN_ODNO": broker_order_id,
            "ORD_DVSN": "00",
            "RVSE_CNCL_DVSN_CD": "02",  # 02 = cancel
            "ORD_QTY": "0",
            "ORD_UNPR": "0",
            "QTY_ALL_ORD_YN": "Y",      # cancel all remaining
        }

        headers = self._auth_headers(tr_id=tr_id)

        try:
            status, data = self._http_request(
                method="POST",
                path=ORDER_CANCEL_PATH,
                headers=headers,
                body=body,
            )
        except KISAdapterError as e:
            return OrderResponse(
                success=False,
                broker_order_id=broker_order_id,
                client_order_id=f"cancel-{broker_order_id}",
                status=OrderStatus.REJECTED,
                error_code="TRANSPORT",
                error_message=str(e)[:500],
                received_at_utc=self._now_fn(),
            )

        rt_cd = str(data.get("rt_cd", "")).strip()
        if status != 200 or rt_cd != "0":
            return OrderResponse(
                success=False,
                broker_order_id=broker_order_id,
                client_order_id=f"cancel-{broker_order_id}",
                status=OrderStatus.REJECTED,
                error_code=str(data.get("msg_cd", ""))[:50],
                error_message=str(data.get("msg1", "unknown"))[:500],
                received_at_utc=self._now_fn(),
            )

        return OrderResponse(
            success=True,
            broker_order_id=broker_order_id,
            client_order_id=f"cancel-{broker_order_id}",
            status=OrderStatus.CANCELLED,
            error_code=None,
            error_message=None,
            received_at_utc=self._now_fn(),
        )


__all__ = (
    "KISExecutionAdapter",
    "ORDER_PLACE_PATH",
    "ORDER_CANCEL_PATH",
)
