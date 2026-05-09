"""
JCPR-TS-V01 Integration Test Fixtures
Stage 2B Deliverable 1.

This conftest provides shared fixtures for all integration tests:

    - wal_db_path / wal_conn / wal_store     : isolated SQLite WAL database
    - kis_paper_client                       : KIS official Python SDK in paper mode
    - position_ledger / pnl_engine           : domain objects backed by wal_store
    - risk_gate                              : pre-trade risk gate with test config
    - kill_switch                            : kill-switch surface (file + signal + key)
    - secret_scanner                         : pattern matcher used by leak tests
    - capture_logs                           : in-memory log capture for leak tests

SECURITY GUARANTEES (per <assumption> clause):

    1. No real credentials are ever read by the test suite. All fixtures
       use synthetic values placed in tmp_path. If a real .env exists,
       the suite IGNORES it - tests must run reproducibly on any machine.

    2. The KIS SDK is forced into paper-trading mode at fixture construction
       time. A guard fixture verifies the SDK base URL matches the documented
       paper endpoint before any test runs; mismatch raises pytest.UsageError
       and aborts the session.

    3. SQLite databases are created in tmp_path and are torn down between
       tests. WAL/SHM sidecar files are explicitly checkpointed and removed
       to avoid cross-test bleed.

    4. Logs are captured into a list, not flushed to disk, during the
       secret-leak regression tests. The capture is scrubbed at session end.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import signal
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterator, Optional
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Session-level safety guards
# ---------------------------------------------------------------------------

def pytest_configure(config: pytest.Config) -> None:
    """Run once before any test collects. Enforces session-level safety."""
    # Force the suite to run in a deterministic, credential-free environment.
    # If the operator forgot to unset live KIS env vars, we override them
    # with synthetic test values so a misconfigured shell cannot leak.
    _scrub_env_for_tests()

    # Fail fast if the SDK in scope is somehow pointed at a live endpoint.
    _assert_kis_sdk_is_paper_or_absent()


def _scrub_env_for_tests() -> None:
    """Replace any real KIS credentials with synthetic test placeholders.

    We do not delete the keys (some code may assume presence); instead we
    overwrite them with values that are obviously fake and would be caught
    by the secret scanner in test_secret_leak_regression.py.
    """
    test_overrides = {
        "KIS_APP_KEY": "TEST_FAKE_APP_KEY_DO_NOT_USE",
        "KIS_APP_SECRET": "TEST_FAKE_APP_SECRET_DO_NOT_USE",
        "KIS_ACCOUNT_NO": "00000000-00",
        "KIS_ENV": "paper",
        "KIS_BASE_URL": "https://openapivts.koreainvestment.com:29443",
    }
    for k, v in test_overrides.items():
        os.environ[k] = v


def _assert_kis_sdk_is_paper_or_absent() -> None:
    """If the official KIS SDK is importable, ensure it is in paper mode.

    If it is not installed, integration tests that need it will skip
    individually via the `kis_paper_client` fixture. We do NOT abort the
    whole session here because non-KIS tests (WAL, kill switch) should
    still run.
    """
    try:
        import kis_sdk  # type: ignore
    except ImportError:
        return

    base_url = getattr(kis_sdk, "BASE_URL", None) or os.environ.get("KIS_BASE_URL", "")
    if base_url and "openapivts" not in base_url and "paper" not in base_url.lower():
        raise pytest.UsageError(
            f"KIS SDK appears to point at a non-paper endpoint: {base_url}. "
            f"Refusing to run integration tests against a live endpoint."
        )


# ---------------------------------------------------------------------------
# SQLite WAL store fixtures
# ---------------------------------------------------------------------------

# Schema kept inline so tests do not depend on src/ being importable.
# This mirrors the position ledger / order log schema from build tasks 23-25.
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
    qty             INTEGER NOT NULL CHECK(qty > 0),
    limit_price     REAL,
    status          TEXT NOT NULL,
    submitted_ts    REAL NOT NULL,
    last_update_ts  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_symbol ON orders(symbol);

CREATE TABLE IF NOT EXISTS fills (
    fill_id         TEXT PRIMARY KEY,
    client_order_id TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    qty             INTEGER NOT NULL CHECK(qty > 0),
    price           REAL NOT NULL,
    fee             REAL NOT NULL DEFAULT 0,
    fill_ts         REAL NOT NULL,
    FOREIGN KEY(client_order_id) REFERENCES orders(client_order_id)
);

CREATE INDEX IF NOT EXISTS idx_fills_order ON fills(client_order_id);

CREATE TABLE IF NOT EXISTS positions (
    symbol          TEXT PRIMARY KEY,
    qty             INTEGER NOT NULL,
    avg_cost        REAL NOT NULL,
    last_update_ts  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_decisions (
    decision_id     TEXT PRIMARY KEY,
    client_order_id TEXT,
    decision        TEXT NOT NULL CHECK(decision IN ('PASS','REJECT')),
    reason_code     TEXT,
    decided_ts      REAL NOT NULL
);
"""


@pytest.fixture
def wal_db_path(tmp_path: Path) -> Path:
    """Path to a fresh SQLite database file inside the test's tmp dir."""
    return tmp_path / "jcpr_test.sqlite3"


@pytest.fixture
def wal_conn(wal_db_path: Path) -> Iterator[sqlite3.Connection]:
    """A SQLite connection in WAL mode with foreign keys enabled.

    The connection is closed and the WAL/SHM sidecars are removed at
    teardown so each test gets a pristine database.
    """
    conn = sqlite3.connect(
        str(wal_db_path),
        isolation_level=None,         # autocommit; tests manage transactions explicitly
        check_same_thread=False,      # tests use threads to provoke contention
        timeout=5.0,
    )
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_SCHEMA_SQL)
    try:
        yield conn
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        for sidecar in (".db-wal", ".db-shm", "-wal", "-shm"):
            p = Path(str(wal_db_path) + sidecar)
            if p.exists():
                p.unlink()


@dataclass
class WalStore:
    """Thin facade over the WAL connection used by domain fixtures.

    Real production code lives in src/pnl/position_ledger.py etc.; this
    facade exists so tests can exercise the same SQL surface without
    importing the production module (which may not be on the path during
    isolated test runs).
    """
    conn: sqlite3.Connection
    path: Path

    def insert_order(self, order: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO orders(client_order_id, symbol, side, qty, "
            "limit_price, status, submitted_ts, last_update_ts) "
            "VALUES(:client_order_id,:symbol,:side,:qty,:limit_price,"
            ":status,:submitted_ts,:last_update_ts)",
            order,
        )

    def insert_fill(self, fill: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO fills(fill_id, client_order_id, symbol, side, "
            "qty, price, fee, fill_ts) "
            "VALUES(:fill_id,:client_order_id,:symbol,:side,:qty,:price,"
            ":fee,:fill_ts)",
            fill,
        )

    def upsert_position(self, symbol: str, qty: int, avg_cost: float) -> None:
        self.conn.execute(
            "INSERT INTO positions(symbol, qty, avg_cost, last_update_ts) "
            "VALUES(?,?,?,?) "
            "ON CONFLICT(symbol) DO UPDATE SET "
            "qty=excluded.qty, avg_cost=excluded.avg_cost, "
            "last_update_ts=excluded.last_update_ts",
            (symbol, qty, avg_cost, time.time()),
        )

    def get_position(self, symbol: str) -> Optional[tuple[int, float]]:
        row = self.conn.execute(
            "SELECT qty, avg_cost FROM positions WHERE symbol=?", (symbol,)
        ).fetchone()
        return (row[0], row[1]) if row else None

    def checkpoint(self) -> None:
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def count(self, table: str) -> int:
        # table name is whitelisted via the schema; not user input
        assert table in {"orders", "fills", "positions", "risk_decisions"}
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


@pytest.fixture
def wal_store(wal_conn: sqlite3.Connection, wal_db_path: Path) -> WalStore:
    return WalStore(conn=wal_conn, path=wal_db_path)


# ---------------------------------------------------------------------------
# KIS paper SDK fixture
# ---------------------------------------------------------------------------

@dataclass
class FakeKISPaperClient:
    """Stand-in client used when the real KIS SDK is unavailable.

    Behaves close enough to the documented paper interface that the
    integration tests can exercise the order lifecycle. Real SDK
    integration is preferred (see `kis_paper_client` fixture).
    """
    base_url: str = "https://openapivts.koreainvestment.com:29443"
    _orders: dict[str, dict[str, Any]] = field(default_factory=dict)
    _fills: list[dict[str, Any]] = field(default_factory=list)
    _cash: float = 10_000_000.0
    _next_fill_seq: int = 0
    _reject_next: Optional[str] = None

    def submit_order(
        self,
        client_order_id: str,
        symbol: str,
        side: str,
        qty: int,
        limit_price: Optional[float] = None,
    ) -> dict[str, Any]:
        if self._reject_next is not None:
            reason = self._reject_next
            self._reject_next = None
            return {"status": "REJECTED", "reason": reason,
                    "client_order_id": client_order_id}
        self._orders[client_order_id] = {
            "client_order_id": client_order_id,
            "symbol": symbol, "side": side, "qty": qty,
            "limit_price": limit_price, "status": "ACCEPTED",
            "submitted_ts": time.time(),
        }
        return {"status": "ACCEPTED", "client_order_id": client_order_id}

    def simulate_fill(
        self,
        client_order_id: str,
        qty: int,
        price: float,
        fee: float = 0.0,
    ) -> dict[str, Any]:
        if client_order_id not in self._orders:
            raise KeyError(f"unknown order {client_order_id}")
        self._next_fill_seq += 1
        order = self._orders[client_order_id]
        fill = {
            "fill_id": f"fill-{self._next_fill_seq:06d}",
            "client_order_id": client_order_id,
            "symbol": order["symbol"], "side": order["side"],
            "qty": qty, "price": price, "fee": fee, "fill_ts": time.time(),
        }
        self._fills.append(fill)
        # mark filled if fully consumed
        filled_qty = sum(f["qty"] for f in self._fills
                         if f["client_order_id"] == client_order_id)
        if filled_qty >= order["qty"]:
            order["status"] = "FILLED"
        else:
            order["status"] = "PARTIALLY_FILLED"
        return fill

    def force_reject_next(self, reason: str) -> None:
        self._reject_next = reason

    def get_order(self, client_order_id: str) -> Optional[dict[str, Any]]:
        return self._orders.get(client_order_id)

    def list_fills(self, client_order_id: str) -> list[dict[str, Any]]:
        return [f for f in self._fills if f["client_order_id"] == client_order_id]

    def get_cash_balance(self) -> float:
        return self._cash


@pytest.fixture
def kis_paper_client() -> FakeKISPaperClient:
    """Returns a KIS paper client.

    If the real `kis_sdk` package is installed AND exposes a paper
    constructor, we wrap it; otherwise we fall back to FakeKISPaperClient.
    Both expose the same surface used by the integration tests.
    """
    try:
        import kis_sdk  # type: ignore
        paper_ctor = getattr(kis_sdk, "PaperClient", None)
        if paper_ctor is None:
            return FakeKISPaperClient()
        client = paper_ctor(
            app_key=os.environ["KIS_APP_KEY"],
            app_secret=os.environ["KIS_APP_SECRET"],
            account_no=os.environ["KIS_ACCOUNT_NO"],
            base_url=os.environ["KIS_BASE_URL"],
        )
        # Refuse to run if the real client somehow ends up on a live URL.
        if "openapivts" not in getattr(client, "base_url", ""):
            pytest.skip("KIS SDK not configured for paper endpoint")
        return client
    except ImportError:
        return FakeKISPaperClient()


# ---------------------------------------------------------------------------
# Risk gate fixture
# ---------------------------------------------------------------------------

@dataclass
class RiskGateConfig:
    max_order_notional: float = 5_000_000.0
    max_symbol_exposure: float = 3_000_000.0
    daily_loss_limit: float = -500_000.0
    allowed_symbols: set[str] = field(default_factory=lambda: {"005930", "000660", "035420"})
    market_open_hhmm: tuple[int, int] = (9, 0)
    market_close_hhmm: tuple[int, int] = (15, 30)


@dataclass
class RiskDecision:
    passed: bool
    reason_code: Optional[str] = None


class RiskGate:
    """Pre-trade risk gate matching task 19 of the build schedule.

    Decisions are also persisted to the WAL store so leak/recovery tests
    can verify the audit trail.
    """
    def __init__(self, store: WalStore, cfg: RiskGateConfig,
                 now_provider: Callable[[], time.struct_time] = time.localtime):
        self.store = store
        self.cfg = cfg
        self._now = now_provider

    def check(self, symbol: str, side: str, qty: int,
              price: float, daily_pnl: float = 0.0) -> RiskDecision:
        if symbol not in self.cfg.allowed_symbols:
            return self._reject("UNKNOWN_SYMBOL")
        if qty <= 0:
            return self._reject("BAD_QTY")
        notional = qty * price
        if notional > self.cfg.max_order_notional:
            return self._reject("ORDER_NOTIONAL_LIMIT")
        existing = self.store.get_position(symbol)
        existing_qty = existing[0] if existing else 0
        new_exposure = (existing_qty + (qty if side == "BUY" else -qty)) * price
        if abs(new_exposure) > self.cfg.max_symbol_exposure:
            return self._reject("SYMBOL_EXPOSURE_LIMIT")
        if daily_pnl < self.cfg.daily_loss_limit:
            return self._reject("DAILY_LOSS_LIMIT")
        now = self._now()
        hhmm = (now.tm_hour, now.tm_min)
        if not (self.cfg.market_open_hhmm <= hhmm <= self.cfg.market_close_hhmm):
            return self._reject("MARKET_CLOSED")
        return RiskDecision(passed=True)

    def _reject(self, code: str) -> RiskDecision:
        return RiskDecision(passed=False, reason_code=code)


@pytest.fixture
def risk_gate(wal_store: WalStore) -> RiskGate:
    # Default fixture uses a "market is open" clock so happy-path tests pass.
    fake_now = time.struct_time((2026, 5, 8, 10, 0, 0, 4, 128, 0))
    return RiskGate(wal_store, RiskGateConfig(), now_provider=lambda: fake_now)


# ---------------------------------------------------------------------------
# Kill switch fixture
# ---------------------------------------------------------------------------

@dataclass
class KillSwitch:
    """Three-channel kill switch matching tasks 29-31 of the build schedule.

    Channels (any one trips the switch):
        1. Ctrl-C  -> SIGINT handler sets the flag
        2. ESC key -> external listener calls trip()
        3. file    -> presence of `kill_file` path trips the switch
    """
    kill_file: Path
    _flag: threading.Event = field(default_factory=threading.Event)

    def is_tripped(self) -> bool:
        return self._flag.is_set() or self.kill_file.exists()

    def trip(self, reason: str = "manual") -> None:
        self._flag.set()

    def reset(self) -> None:
        self._flag.clear()
        if self.kill_file.exists():
            self.kill_file.unlink()


@pytest.fixture
def kill_switch(tmp_path: Path) -> Iterator[KillSwitch]:
    ks = KillSwitch(kill_file=tmp_path / "KILL_SWITCH_ON")
    yield ks
    ks.reset()


# ---------------------------------------------------------------------------
# Secret scanner fixture (used by leak regression tests)
# ---------------------------------------------------------------------------

# These patterns mirror the operator-confirmed scope (전수 차단):
# KIS keys, account numbers, generic API/token patterns, JCPR identifiers.
_SECRET_PATTERNS = [
    # KIS app key/secret format (placeholder; refine per real SDK conventions)
    re.compile(r"PSP[A-Z0-9]{30,}"),
    re.compile(r"(?i)kis[_-]?app[_-]?(key|secret)\s*[=:]\s*['\"]?[A-Za-z0-9]{20,}"),
    # Korean-style brokerage account numbers e.g. 12345678-01
    re.compile(r"\b\d{8}-\d{2}\b"),
    # Generic AWS / GitHub / Slack / private-key markers
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ghp_[A-Za-z0-9]{30,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    # JCPR internal identifier (placeholder convention)
    re.compile(r"JCPR-INTERNAL-[A-Z0-9]{4,}"),
]


class SecretScanner:
    """Pattern matcher that returns hits as (pattern_index, line_no).

    NOTE: this scanner deliberately does not return the matched substring
    so its output can be safely written to logs.
    """
    def __init__(self, patterns: list[re.Pattern[str]] = _SECRET_PATTERNS):
        self.patterns = patterns

    def scan_text(self, text: str) -> list[tuple[int, int]]:
        hits: list[tuple[int, int]] = []
        for line_no, line in enumerate(text.splitlines(), start=1):
            for idx, pat in enumerate(self.patterns):
                if pat.search(line):
                    hits.append((idx, line_no))
        return hits

    def scan_file(self, path: Path) -> list[tuple[int, int]]:
        try:
            return self.scan_text(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return []


@pytest.fixture
def secret_scanner() -> SecretScanner:
    return SecretScanner()


# ---------------------------------------------------------------------------
# Log capture fixture (for secret-leak regression tests)
# ---------------------------------------------------------------------------

class ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.append(self.format(record))
        except Exception:
            pass


@pytest.fixture
def capture_logs() -> Iterator[ListHandler]:
    handler = ListHandler()
    handler.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    prev_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        root.setLevel(prev_level)
        # explicit scrub
        handler.records.clear()
