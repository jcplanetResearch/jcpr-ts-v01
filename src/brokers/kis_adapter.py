"""Task 9 — KIS (Korea Investment & Securities) broker adapter.

Implements BrokerAdapter (read-only) for KIS OpenAPI:
    - Paper:  https://openapivts.koreainvestment.com:29443
    - Prod:   https://openapi.koreainvestment.com:9443

References:
    https://apiportal.koreainvestment.com/apiservice
    https://github.com/koreainvestment/open-trading-api

Security guarantees:
    1. TLS 1.2+ only (KIS will reject 1.0/1.1 after 2025-12-12).
    2. Prod mode requires JCPR_ALLOW_LIVE=1 env var (defense in depth).
    3. Token cached in file with 0600 permissions; auto-refresh 5min before expiry.
    4. Token issuance rate-limited to 1/min (KIS server limit) — local guard.
    5. All credentials masked in any log/repr output.
    6. ESC/Ctrl-C signals propagate via _interrupted flag.
    7. No external dependencies — uses urllib (stdlib only).

This module does NOT implement BrokerExecutionInterface. That is Task 40's job.
"""
from __future__ import annotations

import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from .base import (
    AccountSummary,
    BrokerAdapter,
    BrokerMode,
    ConnectionCheck,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
)
from ._secrets import KISSecrets, SecretValue


# =============================================================================
# Constants
# =============================================================================

#: KIS REST endpoints. Tuple immutable.
KIS_PAPER_BASE_URL: str = "https://openapivts.koreainvestment.com:29443"
KIS_PROD_BASE_URL: str = "https://openapi.koreainvestment.com:9443"

#: Token request endpoint (same path on both modes).
TOKEN_PATH: str = "/oauth2/tokenP"

#: Account inquiry paths.
ACCOUNT_BALANCE_PATH: str = "/uapi/domestic-stock/v1/trading/inquire-balance"
ORDER_LIST_PATH: str = "/uapi/domestic-stock/v1/trading/inquire-daily-ccld"

#: TR_IDs differ between paper and prod (KIS convention).
TR_ID_BALANCE_PAPER: str = "VTTC8434R"
TR_ID_BALANCE_PROD: str = "TTTC8434R"
TR_ID_ORDER_LIST_PAPER: str = "VTTC0081R"
TR_ID_ORDER_LIST_PROD: str = "TTTC0081R"

#: HTTP defaults.
HTTP_TIMEOUT_SEC: int = 10
TOKEN_REFRESH_BUFFER_SEC: int = 300  # refresh 5min before expiry

#: Min time between token issuances (KIS server enforces 1/min).
MIN_TOKEN_ISSUE_INTERVAL_SEC: int = 60

#: Token cache file mode (0600 — owner only).
TOKEN_FILE_MODE: int = 0o600

#: User-Agent for all requests. KIS recommends keeping default-like.
USER_AGENT: str = "Mozilla/5.0 (compatible; jcpr-ts-v01)"

#: Live-mode safety env var. Must be "1" to allow prod adapter construction.
LIVE_MODE_ENV_VAR: str = "JCPR_ALLOW_LIVE"


class KISAdapterError(RuntimeError):
    """Raised on any KIS adapter failure."""


# =============================================================================
# TLS context — enforce 1.2+ minimum
# =============================================================================

def _build_secure_tls_context() -> ssl.SSLContext:
    """Build SSL context that refuses TLS < 1.2.

    KIS requires TLS 1.2+ as of 2025-12-12. We enforce this client-side too
    for defense in depth — if KIS adds a vulnerable cipher, we still refuse.
    """
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    # Require certificate verification (default but make it explicit)
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.check_hostname = True
    return ctx


# =============================================================================
# Token cache — file-based with 0600 perms
# =============================================================================

class _TokenCache:
    """Thread-safe token cache backed by a 0600-mode JSON file.

    Format:
        {
          "access_token": "...",
          "issued_at_utc": "2026-05-07T12:00:00+00:00",
          "expires_at_utc": "2026-05-08T12:00:00+00:00",
          "mode": "paper"
        }
    """

    def __init__(self, cache_path: Path, mode: BrokerMode) -> None:
        self._path = cache_path
        self._mode = mode
        self._lock = threading.Lock()
        self._cached_token: str | None = None
        self._expires_at: datetime | None = None

    def load(self) -> tuple[str | None, datetime | None]:
        """Load cached token if file exists and is valid for this mode."""
        with self._lock:
            if self._cached_token is not None:
                return self._cached_token, self._expires_at
            if not self._path.exists():
                return None, None

            # Verify file permissions
            if os.name == "posix":
                file_mode = self._path.stat().st_mode & 0o777
                if file_mode & 0o077:
                    # Insecure perms — refuse to load. Caller should re-issue.
                    return None, None

            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None, None

            # Mode mismatch → ignore cache
            if data.get("mode") != self._mode.value:
                return None, None

            try:
                expires = datetime.fromisoformat(data["expires_at_utc"])
            except (KeyError, ValueError):
                return None, None

            token = data.get("access_token")
            if not isinstance(token, str) or not token:
                return None, None

            self._cached_token = token
            self._expires_at = expires
            return token, expires

    def save(self, *, token: str, expires_at_utc: datetime) -> None:
        """Persist token with 0600 file mode."""
        with self._lock:
            payload = {
                "access_token": token,
                "issued_at_utc": datetime.now(tz=timezone.utc).isoformat(),
                "expires_at_utc": expires_at_utc.isoformat(),
                "mode": self._mode.value,
            }
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # Write to temp file then rename for atomicity
            tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp_path.write_text(json.dumps(payload), encoding="utf-8")
            if os.name == "posix":
                os.chmod(tmp_path, TOKEN_FILE_MODE)
            tmp_path.replace(self._path)
            self._cached_token = token
            self._expires_at = expires_at_utc


# =============================================================================
# Helpers
# =============================================================================

def _to_decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    """Robust Decimal conversion."""
    if value is None or value == "":
        return default
    if isinstance(value, Decimal):
        return value
    try:
        # KIS sometimes returns numbers with leading zeros / commas
        s = str(value).replace(",", "").strip()
        if not s:
            return default
        return Decimal(s)
    except Exception:
        return default


def _parse_kis_datetime(date_str: str, time_str: str) -> datetime:
    """Parse KIS date+time fields (YYYYMMDD + HHMMSS) into UTC datetime.

    KIS server time is KST (UTC+9). We convert to UTC for internal storage.
    """
    if not date_str or not time_str:
        return datetime.now(tz=timezone.utc)
    try:
        date_str = date_str.strip()
        time_str = time_str.strip().zfill(6)
        kst_naive = datetime.strptime(f"{date_str}{time_str}", "%Y%m%d%H%M%S")
        kst = timezone(timedelta(hours=9))
        return kst_naive.replace(tzinfo=kst).astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return datetime.now(tz=timezone.utc)


# =============================================================================
# KIS adapter
# =============================================================================

class KISBrokerAdapter(BrokerAdapter):
    """Read-only KIS broker adapter. See BrokerAdapter docstring.

    Construction:
        adapter = KISBrokerAdapter(
            secrets=load_kis_secrets(env_path=".env", mode="paper"),
            mode=BrokerMode.PAPER,
            token_cache_path=Path("./runtime/.kis_token_paper.json"),
        )

    For PROD mode, JCPR_ALLOW_LIVE=1 env var MUST be set or construction fails.
    """

    def __init__(
        self,
        *,
        secrets: KISSecrets,
        mode: BrokerMode | str,
        token_cache_path: str | os.PathLike[str] | None = None,
        http_timeout_sec: int = HTTP_TIMEOUT_SEC,
        _ssl_context: ssl.SSLContext | None = None,
        _now_fn: Any = None,  # injectable for tests
        _opener: Any = None,  # injectable for tests
    ) -> None:
        if isinstance(mode, str):
            mode = BrokerMode(mode)
        if not isinstance(secrets, KISSecrets):
            raise TypeError("secrets must be KISSecrets")
        if secrets.mode != mode.value:
            raise KISAdapterError(
                f"secrets.mode ({secrets.mode}) != adapter mode ({mode.value})"
            )

        # PROD safety check — defense in depth
        if mode == BrokerMode.PROD:
            allow_live = os.environ.get(LIVE_MODE_ENV_VAR, "")
            if allow_live != "1":
                raise KISAdapterError(
                    f"PROD mode requires {LIVE_MODE_ENV_VAR}=1 environment "
                    f"variable. Refusing to construct prod adapter."
                )

        self._mode = mode
        self._secrets = secrets
        self._base_url = (
            KIS_PROD_BASE_URL if mode == BrokerMode.PROD else KIS_PAPER_BASE_URL
        )
        self._timeout = http_timeout_sec
        self._ssl_context = _ssl_context or _build_secure_tls_context()
        self._now_fn = _now_fn or (lambda: datetime.now(tz=timezone.utc))
        self._opener = _opener  # if provided, replaces urllib.request

        if token_cache_path is None:
            token_cache_path = Path(f"./runtime/.kis_token_{mode.value}.json")
        self._token_cache = _TokenCache(Path(token_cache_path), mode)
        self._last_token_issue_at: datetime | None = None
        self._token_issue_lock = threading.Lock()

        # Interrupt flag — set by signal handlers (Tasks 29, 30)
        self._interrupted = threading.Event()

    # -------------------------------------------------------------------------
    # BrokerAdapter properties
    # -------------------------------------------------------------------------

    @property
    def mode(self) -> BrokerMode:
        return self._mode

    @property
    def adapter_name(self) -> str:
        return "kis"

    @property
    def base_url(self) -> str:
        return self._base_url

    def signal_interrupt(self) -> None:
        """Signal handlers (Task 29/30) call this to abort in-flight calls."""
        self._interrupted.set()

    # -------------------------------------------------------------------------
    # Internal HTTP
    # -------------------------------------------------------------------------

    def _http_request(
        self,
        *,
        method: str,
        path: str,
        headers: Mapping[str, str] | None = None,
        body: Mapping[str, Any] | None = None,
        query: Mapping[str, str] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        """Issue an HTTP request with TLS 1.2+ and short timeout.

        Returns (status_code, parsed_json_body).
        Raises KISAdapterError on transport failure or bad JSON.
        """
        if self._interrupted.is_set():
            raise KISAdapterError("interrupted by ESC/Ctrl-C signal")

        url = self._base_url + path
        if query:
            from urllib.parse import urlencode
            url += "?" + urlencode(query)

        req_body: bytes | None = None
        all_headers = {
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
        if headers:
            all_headers.update(headers)
        if body is not None:
            req_body = json.dumps(body).encode("utf-8")

        request = urllib.request.Request(
            url=url,
            data=req_body,
            headers=all_headers,
            method=method,
        )

        try:
            if self._opener is not None:
                # Test-injected opener
                response = self._opener(request, timeout=self._timeout)
            else:
                response = urllib.request.urlopen(
                    request,
                    timeout=self._timeout,
                    context=self._ssl_context,
                )
            status = response.getcode()
            raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            status = e.code
            try:
                raw = e.read().decode("utf-8")
            except Exception:
                raw = ""
        except urllib.error.URLError as e:
            raise KISAdapterError(f"network error: {e.reason}") from e
        except (TimeoutError, OSError) as e:
            raise KISAdapterError(f"request failed: {e}") from e

        if not raw:
            return status, {}
        try:
            return status, json.loads(raw)
        except json.JSONDecodeError as e:
            raise KISAdapterError(f"invalid JSON response: {e}") from e

    # -------------------------------------------------------------------------
    # Token management
    # -------------------------------------------------------------------------

    def _ensure_access_token(self) -> tuple[str, datetime]:
        """Return a valid access token, refreshing if needed."""
        now = self._now_fn()

        # Try cache first
        token, expires = self._token_cache.load()
        if token and expires:
            if expires > now + timedelta(seconds=TOKEN_REFRESH_BUFFER_SEC):
                return token, expires

        # Need to issue a new token — rate limit check
        with self._token_issue_lock:
            # Re-check cache inside lock (race avoidance)
            token, expires = self._token_cache.load()
            if token and expires and expires > now + timedelta(
                seconds=TOKEN_REFRESH_BUFFER_SEC
            ):
                return token, expires

            if self._last_token_issue_at is not None:
                elapsed = (now - self._last_token_issue_at).total_seconds()
                if elapsed < MIN_TOKEN_ISSUE_INTERVAL_SEC:
                    raise KISAdapterError(
                        f"token issue rate limit — wait "
                        f"{MIN_TOKEN_ISSUE_INTERVAL_SEC - int(elapsed)}s"
                    )

            new_token, new_expires = self._issue_token()
            self._last_token_issue_at = now
            self._token_cache.save(token=new_token, expires_at_utc=new_expires)
            return new_token, new_expires

    def _issue_token(self) -> tuple[str, datetime]:
        """Call /oauth2/tokenP to issue a fresh access token."""
        body = {
            "grant_type": "client_credentials",
            "appkey": self._secrets.appkey.reveal(),
            "appsecret": self._secrets.appsecret.reveal(),
        }
        status, data = self._http_request(
            method="POST",
            path=TOKEN_PATH,
            body=body,
        )
        if status != 200:
            err_msg = data.get("error_description") or data.get("msg1") or "unknown"
            raise KISAdapterError(
                f"token issuance failed (status={status}): {err_msg}"
            )
        token = data.get("access_token")
        expires_in = data.get("expires_in", 86400)
        if not isinstance(token, str) or not token:
            raise KISAdapterError("token response missing access_token")
        try:
            expires_in_sec = int(expires_in)
        except (ValueError, TypeError):
            expires_in_sec = 86400
        expires_at = self._now_fn() + timedelta(seconds=expires_in_sec)
        return token, expires_at

    def _auth_headers(self, *, tr_id: str) -> dict[str, str]:
        """Build authentication headers for a TR-bound request."""
        token, _ = self._ensure_access_token()
        return {
            "authorization": f"Bearer {token}",
            "appkey": self._secrets.appkey.reveal(),
            "appsecret": self._secrets.appsecret.reveal(),
            "tr_id": tr_id,
            "custtype": "P",  # personal account
        }

    # -------------------------------------------------------------------------
    # BrokerAdapter implementation
    # -------------------------------------------------------------------------

    def check_connection(self) -> ConnectionCheck:
        """Verify connectivity, TLS, and token validity. Never raises."""
        start = time.monotonic()
        now = self._now_fn()
        try:
            token, expires = self._ensure_access_token()
            elapsed_ms = int((time.monotonic() - start) * 1000)
            return ConnectionCheck(
                success=True,
                mode=self._mode,
                base_url=self._base_url,
                tls_version=self._ssl_context.minimum_version.name,
                token_valid=True,
                token_expires_at_utc=expires,
                server_time_utc=now,
                error_message=None,
                elapsed_ms=elapsed_ms,
            )
        except Exception as e:  # noqa: BLE001 — must never raise
            elapsed_ms = int((time.monotonic() - start) * 1000)
            return ConnectionCheck(
                success=False,
                mode=self._mode,
                base_url=self._base_url,
                tls_version=self._ssl_context.minimum_version.name,
                token_valid=False,
                token_expires_at_utc=None,
                server_time_utc=None,
                error_message=str(e)[:500],
                elapsed_ms=elapsed_ms,
            )

    def get_account_summary(self) -> AccountSummary:
        """Fetch account balance via inquire-balance endpoint."""
        tr_id = (
            TR_ID_BALANCE_PROD if self._mode == BrokerMode.PROD
            else TR_ID_BALANCE_PAPER
        )
        headers = self._auth_headers(tr_id=tr_id)
        query = {
            "CANO": self._secrets.account_number,
            "ACNT_PRDT_CD": self._secrets.account_product,
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "02",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "01",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }
        status, data = self._http_request(
            method="GET",
            path=ACCOUNT_BALANCE_PATH,
            headers=headers,
            query=query,
        )
        if status != 200:
            raise KISAdapterError(
                f"account balance failed (status={status}): "
                f"{data.get('msg1', 'unknown')}"
            )

        # KIS response shape: {"output1": [positions...], "output2": [summary...]}
        summary_list = data.get("output2", [])
        if not summary_list:
            raise KISAdapterError("account balance: empty output2")
        summary = summary_list[0] if isinstance(summary_list, list) else summary_list

        cash = _to_decimal(summary.get("dnca_tot_amt"))
        equity = _to_decimal(summary.get("tot_evlu_amt"))
        buying_power = _to_decimal(summary.get("ord_psbl_cash") or summary.get("nxdy_excc_amt"))

        return AccountSummary(
            account_id_masked=self._secrets.account_masked,
            cash_balance_krw=cash,
            total_equity_krw=equity,
            buying_power_krw=buying_power,
            mode=self._mode,
            fetched_at_utc=self._now_fn(),
        )

    def get_positions(self) -> tuple[Position, ...]:
        """Fetch positions from inquire-balance output1."""
        tr_id = (
            TR_ID_BALANCE_PROD if self._mode == BrokerMode.PROD
            else TR_ID_BALANCE_PAPER
        )
        headers = self._auth_headers(tr_id=tr_id)
        query = {
            "CANO": self._secrets.account_number,
            "ACNT_PRDT_CD": self._secrets.account_product,
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "02",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "01",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }
        status, data = self._http_request(
            method="GET",
            path=ACCOUNT_BALANCE_PATH,
            headers=headers,
            query=query,
        )
        if status != 200:
            raise KISAdapterError(
                f"positions fetch failed (status={status})"
            )

        rows = data.get("output1", [])
        if not isinstance(rows, list):
            return ()

        positions: list[Position] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            qty = _to_decimal(row.get("hldg_qty"))
            if qty == Decimal("0"):
                continue  # skip flat positions
            try:
                pos = Position(
                    symbol=str(row.get("pdno", "")).strip(),
                    quantity=qty,
                    avg_cost_krw=_to_decimal(row.get("pchs_avg_pric")),
                    current_price_krw=_to_decimal(row.get("prpr")),
                    market_value_krw=_to_decimal(row.get("evlu_amt")),
                    unrealized_pnl_krw=_to_decimal(row.get("evlu_pfls_amt")),
                )
            except (ValueError, TypeError):
                continue
            positions.append(pos)
        return tuple(positions)

    def get_orders(
        self,
        *,
        status: OrderStatus | None = None,
        symbol: str | None = None,
        limit: int = 50,
    ) -> tuple[Order, ...]:
        """Fetch today's order list. KIS provides daily order/execution log."""
        if limit < 1 or limit > 200:
            raise ValueError("limit must be 1..200")

        tr_id = (
            TR_ID_ORDER_LIST_PROD if self._mode == BrokerMode.PROD
            else TR_ID_ORDER_LIST_PAPER
        )
        headers = self._auth_headers(tr_id=tr_id)
        today = self._now_fn().strftime("%Y%m%d")
        query: dict[str, str] = {
            "CANO": self._secrets.account_number,
            "ACNT_PRDT_CD": self._secrets.account_product,
            "INQR_STRT_DT": today,
            "INQR_END_DT": today,
            "SLL_BUY_DVSN_CD": "00",  # all
            "INQR_DVSN": "00",
            "PDNO": symbol or "",
            "CCLD_DVSN": "00",
            "ORD_GNO_BRNO": "",
            "ODNO": "",
            "INQR_DVSN_3": "00",
            "INQR_DVSN_1": "",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }
        http_status, data = self._http_request(
            method="GET",
            path=ORDER_LIST_PATH,
            headers=headers,
            query=query,
        )
        if http_status != 200:
            raise KISAdapterError(
                f"order list fetch failed (status={http_status})"
            )

        rows = data.get("output1", [])
        if not isinstance(rows, list):
            return ()

        orders: list[Order] = []
        for row in rows[:limit]:
            if not isinstance(row, Mapping):
                continue
            try:
                order = self._parse_order_row(row)
            except (ValueError, TypeError, KeyError):
                continue
            if status is not None and order.status != status:
                continue
            orders.append(order)
        return tuple(orders)

    def _parse_order_row(self, row: Mapping[str, Any]) -> Order:
        """Translate KIS order row → Order."""
        side = OrderSide.BUY if str(row.get("sll_buy_dvsn_cd", "")).strip() == "02" \
            else OrderSide.SELL
        ord_dvsn = str(row.get("ord_dvsn_cd", "00")).strip()
        order_type = OrderType.LIMIT if ord_dvsn == "00" else OrderType.MARKET
        kis_status = str(row.get("ccld_dvsn", "")).strip()  # 체결 구분
        # Status mapping (KIS uses several fields; we use ccld_dvsn primarily)
        ord_qty = _to_decimal(row.get("ord_qty"))
        tot_ccld_qty = _to_decimal(row.get("tot_ccld_qty"))
        cancel_yn = str(row.get("cncl_yn", "N")).strip().upper()
        if cancel_yn == "Y":
            status = OrderStatus.CANCELLED
        elif tot_ccld_qty >= ord_qty and ord_qty > Decimal("0"):
            status = OrderStatus.FILLED
        elif tot_ccld_qty > Decimal("0"):
            status = OrderStatus.PARTIALLY_FILLED
        else:
            status = OrderStatus.PENDING

        limit_price = _to_decimal(row.get("ord_unpr"))
        avg_fill = _to_decimal(row.get("avg_prvs"))
        placed_at = _parse_kis_datetime(
            row.get("ord_dt", ""), row.get("ord_tmd", "")
        )

        return Order(
            order_id=str(row.get("odno", "")).strip(),
            symbol=str(row.get("pdno", "")).strip(),
            side=side,
            order_type=order_type,
            quantity=ord_qty,
            filled_quantity=tot_ccld_qty,
            limit_price_krw=limit_price if limit_price > Decimal("0") else None,
            avg_fill_price_krw=avg_fill if avg_fill > Decimal("0") else None,
            status=status,
            placed_at_utc=placed_at,
            last_updated_utc=placed_at,
        )

    # -------------------------------------------------------------------------
    # Repr (PII-safe)
    # -------------------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"KISBrokerAdapter(mode={self._mode.value}, "
            f"account={self._secrets.account_masked}, "
            f"appkey={self._secrets.appkey_masked})"
        )


__all__ = (
    "KISBrokerAdapter",
    "KISAdapterError",
    "KIS_PAPER_BASE_URL",
    "KIS_PROD_BASE_URL",
    "LIVE_MODE_ENV_VAR",
)
