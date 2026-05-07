"""
스모크 테스트 — data_loader (Smoke Tests)
==========================================

JCPR Trading System - jcpr-ts-v01
Task 48 v0.1.1

실행 (Run):
    python -m pytest tests/dashboard/test_data_loader.py -v
또는:
    python tests/dashboard/test_data_loader.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# repo root path
_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from src.dashboard.data_loader import (  # noqa: E402
    DashboardDataSource,
    _read_jsonl_to_df,
    _safe_query,
    _table_exists,
    load_audit_summary,
    load_fills,
    load_kill_switch_status,
    load_market_status,
    load_pnl_snapshot,
    load_positions,
    load_rejection_summary,
)


# ─────────────────────────────────────────────────
# 테스트 픽스처 (Test Fixtures)
# ─────────────────────────────────────────────────

def _make_positions_db(path: str) -> None:
    """테스트용 positions DB 생성."""
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE positions (
                symbol TEXT PRIMARY KEY,
                quantity REAL,
                avg_cost_krw REAL,
                side TEXT,
                opened_at_utc TEXT
            );
            CREATE TABLE fills (
                fill_id TEXT PRIMARY KEY,
                symbol TEXT,
                side TEXT,
                quantity REAL,
                price_krw REAL,
                gross_krw REAL,
                fee_krw REAL,
                tax_krw REAL,
                filled_at_utc TEXT
            );
            CREATE TABLE realized_pnl (
                pnl_id TEXT PRIMARY KEY,
                symbol TEXT,
                realized_pnl_krw REAL,
                realized_at_utc TEXT
            );
            INSERT INTO positions VALUES
                ('005930', 100, 70000, 'long', '2026-05-07T01:00:00+00:00'),
                ('035420', 50, 200000, 'long', '2026-05-07T01:30:00+00:00');
            INSERT INTO fills VALUES
                ('f1', '005930', 'buy', 100, 70000, 7000000, 700, 0, '2026-05-07T01:00:00+00:00'),
                ('f2', '035420', 'buy', 50, 200000, 10000000, 1000, 0, '2026-05-07T01:30:00+00:00');
            INSERT INTO realized_pnl VALUES
                ('rp1', '005930', 50000, '2026-05-07T02:00:00+00:00');
        """)


def _make_ohlcv_db(path: str) -> None:
    """테스트용 OHLCV DB."""
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE ohlcv_daily (
                symbol TEXT,
                date TEXT,
                open_krw REAL,
                high_krw REAL,
                low_krw REAL,
                close_krw REAL,
                volume INTEGER,
                PRIMARY KEY (symbol, date)
            );
            INSERT INTO ohlcv_daily VALUES
                ('005930', '2026-05-07', 70000, 72000, 69000, 71000, 1000000),
                ('035420', '2026-05-07', 200000, 205000, 198000, 202000, 500000);
        """)


def _make_risk_audit(path: str) -> None:
    """테스트용 리스크 감사 JSONL."""
    now = datetime.now(timezone.utc)
    records = [
        {
            "evaluated_at_utc": (now - timedelta(minutes=30)).isoformat(),
            "decision": "approve",
            "symbol": "005930",
        },
        {
            "evaluated_at_utc": (now - timedelta(minutes=20)).isoformat(),
            "decision": "reject",
            "rejected_gate": "position_size_limit",
            "rejection_reason": "exceeds_max_position",
            "symbol": "035420",
        },
        {
            "evaluated_at_utc": (now - timedelta(minutes=10)).isoformat(),
            "decision": "reject",
            "rejected_gate": "daily_loss_limit",
            "rejection_reason": "daily_loss_exceeded",
            "symbol": "005930",
        },
        {
            "evaluated_at_utc": (now - timedelta(minutes=5)).isoformat(),
            "decision": "approve",
            "symbol": "005930",
        },
    ]
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


# ─────────────────────────────────────────────────
# 테스트 함수 (Test Functions)
# ─────────────────────────────────────────────────

def test_safe_query_missing_file():
    """없는 파일 → 빈 DataFrame."""
    df = _safe_query("/tmp/nonexistent_xyz.db", "SELECT 1")
    assert df.empty, "Missing file should return empty DataFrame"
    print("✅ test_safe_query_missing_file")


def test_table_exists_missing_file():
    """없는 파일 → False."""
    assert not _table_exists("/tmp/nonexistent_xyz.db", "any")
    print("✅ test_table_exists_missing_file")


def test_load_positions_empty():
    """빈 인자 → 빈 DataFrame."""
    df = load_positions(None)
    assert df.empty
    df2 = load_positions("")
    assert df2.empty
    print("✅ test_load_positions_empty")


def test_load_positions_with_data(tmp_db_paths):
    """테스트 DB에서 정상 로드."""
    pos_db, _ = tmp_db_paths
    df = load_positions(pos_db)
    assert len(df) == 2, f"Expected 2 positions, got {len(df)}"
    assert "005930" in df["symbol"].values
    print("✅ test_load_positions_with_data")


def test_load_fills_with_data(tmp_db_paths):
    """체결 로드."""
    pos_db, _ = tmp_db_paths
    df = load_fills(pos_db)
    assert len(df) == 2
    assert df["fee_krw"].sum() == 1700
    print("✅ test_load_fills_with_data")


def test_load_pnl_snapshot(tmp_db_paths):
    """PnL 스냅샷 계산."""
    pos_db, ohlcv_db = tmp_db_paths
    pnl = load_pnl_snapshot(
        pos_db, ohlcv_db, None,
        starting_capital_krw=20_000_000,
        cash_krw=3_000_000,
    )
    assert "error" not in pnl, f"Got error: {pnl.get('error')}"
    # 005930: 100주 × (71000 - 70000) = +100,000
    # 035420: 50주 × (202000 - 200000) = +100,000
    # Total unrealized = 200,000
    assert pnl["unrealized_pnl_krw"] == 200_000, f"Got {pnl['unrealized_pnl_krw']}"
    assert pnl["realized_pnl_krw"] == 50_000
    assert pnl["total_fees_krw"] == 1700
    assert len(pnl["symbol_attribution"]) == 2
    print("✅ test_load_pnl_snapshot")


def test_load_rejection_summary_empty():
    """없는 경로 → error."""
    r = load_rejection_summary(None)
    assert "error" in r
    print("✅ test_load_rejection_summary_empty")


def test_load_rejection_summary_with_data(tmp_audit_path):
    """리스크 감사 JSONL 분석."""
    r = load_rejection_summary(tmp_audit_path)
    assert "error" not in r, f"Got error: {r.get('error')}"
    s = r["summary"]
    assert s["total_evaluations"] == 4
    assert s["reject_count"] == 2
    assert s["rejection_rate"] == 0.5
    assert "position_size_limit" in s["by_gate"]
    # 50% 거부율 → critical 진단
    findings = r["diagnostic_findings"]
    assert any(f["severity"] == "critical" for f in findings)
    print("✅ test_load_rejection_summary_with_data")


def test_load_market_status():
    """시장 상태 — 항상 유효한 dict."""
    s = load_market_status()
    assert "error" not in s
    assert s["state"] in (
        "regular", "pre_market", "after_hours",
        "closed_weekend", "closed_holiday",
    )
    assert isinstance(s["is_open"], bool)
    print("✅ test_load_market_status")


def test_kill_switch_missing():
    """없는 파일 → False."""
    assert not load_kill_switch_status(None)
    assert not load_kill_switch_status("/tmp/nonexistent_kill_switch_xyz")
    print("✅ test_kill_switch_missing")


def test_kill_switch_active(tmp_path_factory):
    """파일 존재 → True."""
    f = tmp_path_factory.mktemp("ks") / "KILL_SWITCH_ON"
    f.write_text("active")
    assert load_kill_switch_status(str(f))
    print("✅ test_kill_switch_active")


def test_audit_summary_empty():
    """없는 경로 → 빈 DataFrame."""
    df = load_audit_summary(None)
    assert df.empty
    print("✅ test_audit_summary_empty")


def test_dashboard_data_source_immutable():
    """frozen dataclass — 변경 불가."""
    ds = DashboardDataSource(positions_db="/tmp/x.db")
    try:
        ds.positions_db = "/tmp/y.db"  # type: ignore[misc]
        assert False, "Should raise FrozenInstanceError"
    except Exception:
        pass
    print("✅ test_dashboard_data_source_immutable")


# ─────────────────────────────────────────────────
# 픽스처 (pytest 호환 + 단독 실행)
# ─────────────────────────────────────────────────

try:
    import pytest

    @pytest.fixture
    def tmp_db_paths(tmp_path):
        pos = str(tmp_path / "positions.db")
        ohlcv = str(tmp_path / "ohlcv.db")
        _make_positions_db(pos)
        _make_ohlcv_db(ohlcv)
        return pos, ohlcv

    @pytest.fixture
    def tmp_audit_path(tmp_path):
        p = str(tmp_path / "risk_audit.jsonl")
        _make_risk_audit(p)
        return p
except ImportError:
    pass


# ─────────────────────────────────────────────────
# 단독 실행 (Standalone Run)
# ─────────────────────────────────────────────────

def _run_all_standalone() -> int:
    """pytest 없이 실행 — 모든 테스트 수동 호출."""
    failed = 0
    with tempfile.TemporaryDirectory() as td:
        pos = str(Path(td) / "positions.db")
        ohlcv = str(Path(td) / "ohlcv.db")
        audit = str(Path(td) / "risk_audit.jsonl")
        _make_positions_db(pos)
        _make_ohlcv_db(ohlcv)
        _make_risk_audit(audit)

        # 픽스처 없는 테스트
        for fn in [
            test_safe_query_missing_file,
            test_table_exists_missing_file,
            test_load_positions_empty,
            test_load_rejection_summary_empty,
            test_load_market_status,
            test_kill_switch_missing,
            test_audit_summary_empty,
            test_dashboard_data_source_immutable,
        ]:
            try:
                fn()
            except AssertionError as e:
                print(f"❌ {fn.__name__}: {e}")
                failed += 1
            except Exception as e:  # noqa: BLE001
                print(f"💥 {fn.__name__}: {type(e).__name__}: {e}")
                failed += 1

        # 픽스처 필요 테스트
        for fn, args in [
            (test_load_positions_with_data, ((pos, ohlcv),)),
            (test_load_fills_with_data, ((pos, ohlcv),)),
            (test_load_pnl_snapshot, ((pos, ohlcv),)),
            (test_load_rejection_summary_with_data, (audit,)),
        ]:
            try:
                fn(*args)
            except AssertionError as e:
                print(f"❌ {fn.__name__}: {e}")
                failed += 1
            except Exception as e:  # noqa: BLE001
                print(f"💥 {fn.__name__}: {type(e).__name__}: {e}")
                failed += 1

        # kill_switch_active (파일 생성)
        try:
            ks = Path(td) / "KILL_SWITCH_ON"
            ks.write_text("active")
            assert load_kill_switch_status(str(ks))
            print("✅ test_kill_switch_active")
        except AssertionError as e:
            print(f"❌ test_kill_switch_active: {e}")
            failed += 1

    return failed


if __name__ == "__main__":
    print("─" * 50)
    print("Task 48 v0.1.1 — data_loader 스모크 테스트")
    print("─" * 50)
    failed = _run_all_standalone()
    print("─" * 50)
    if failed == 0:
        print("✅ 모든 테스트 통과 (All tests passed)")
        sys.exit(0)
    else:
        print(f"❌ {failed}개 테스트 실패 (failed)")
        sys.exit(1)
