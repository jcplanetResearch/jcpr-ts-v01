"""
스모크 테스트 — _tool_handlers
==============================

JCPR Trading System - jcpr-ts-v01
Task 34 v0.1
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import textwrap
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from src.mcp_servers._config import ReadOnlyServerConfig  # noqa: E402
from src.mcp_servers import _tool_handlers as h  # noqa: E402


# ─────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────

def _make_positions_db(path: Path) -> None:
    """테스트용 positions DB 생성."""
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript("""
            CREATE TABLE positions (
                symbol TEXT PRIMARY KEY,
                qty INTEGER,
                avg_cost_krw REAL,
                market_value_krw REAL
            );
            CREATE TABLE fills (
                fill_id TEXT PRIMARY KEY,
                symbol TEXT,
                side TEXT,
                qty INTEGER,
                price_krw REAL,
                timestamp_utc TEXT
            );
            INSERT INTO positions VALUES ('005930', 100, 70000, 7100000);
            INSERT INTO positions VALUES ('000660', 50, 130000, 6500000);
            INSERT INTO positions VALUES ('035420', 0, 0, 0);  -- 청산됨
            INSERT INTO fills VALUES
                ('F1', '005930', 'buy', 100, 70000, '2026-05-07T01:00:00+00:00'),
                ('F2', '000660', 'buy', 50, 130000, '2026-05-07T02:00:00+00:00');
        """)
        conn.commit()
    finally:
        conn.close()


def _make_risk_audit(path: Path) -> None:
    """테스트용 risk audit JSONL."""
    events = [
        {"timestamp_utc": "2026-05-07T01:00:00+00:00",
         "decision": "approve", "gate": "exposure"},
        {"timestamp_utc": "2026-05-07T02:00:00+00:00",
         "decision": "reject", "gate": "kill_switch", "reason": "system_paused"},
        {"timestamp_utc": "2026-05-07T03:00:00+00:00",
         "decision": "reject", "gate": "exposure", "reason": "position_limit"},
        {"timestamp_utc": "2026-05-07T04:00:00+00:00",
         "decision": "reject", "gate": "kill_switch", "reason": "system_paused"},
    ]
    with path.open("w") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


def _make_strategy_yaml(path: Path) -> None:
    """테스트용 strategy_registry.yaml."""
    content = textwrap.dedent("""
    version: "1.0"
    strategies:
      - strategy_id: momentum_v1
        module_path: src.signals.strategies.momentum_v1
        class_name: MomentumV1
        version: "1.0.0"
        enabled: true
        paper_only: false
        capital_weight: 0.6
        max_capital_pct: 0.3
        timeframe: "1d"
        signal_categories: ["ENTRY", "EXIT"]
    """).strip()
    path.write_text(content)


def _config(tmp_dir: Path, *, with_db: bool = False, with_risk: bool = False,
            with_registry: bool = False) -> ReadOnlyServerConfig:
    audit_dir = tmp_dir / "audit"
    audit_dir.mkdir(exist_ok=True)
    kwargs = {
        "audit_dir": str(audit_dir),
        "session_id": "test-session",
    }
    if with_db:
        db = tmp_dir / "positions.sqlite"
        _make_positions_db(db)
        kwargs["positions_db"] = str(db)
    if with_risk:
        ra = tmp_dir / "risk.jsonl"
        _make_risk_audit(ra)
        kwargs["risk_audit_path"] = str(ra)
    if with_registry:
        reg = tmp_dir / "strategy_registry.yaml"
        _make_strategy_yaml(reg)
        kwargs["strategy_registry_path"] = str(reg)
    return ReadOnlyServerConfig(**kwargs)


# ─────────────────────────────────────────────────
# Tool 1: get_market_status
# ─────────────────────────────────────────────────

def test_market_status(tmp_dir):
    cfg = _config(tmp_dir)
    res = h.get_market_status(cfg)
    assert res["ok"] is True
    assert res["market"] == "KRX"
    assert res["state"] in (
        "open", "closed", "closed_weekend", "pre_market", "closed_post"
    )
    assert "kst_time" in res
    print("✅ test_market_status")


# ─────────────────────────────────────────────────
# Tool 2: get_positions
# ─────────────────────────────────────────────────

def test_positions_no_db(tmp_dir):
    cfg = _config(tmp_dir, with_db=False)
    res = h.get_positions(cfg)
    assert res["ok"] is True
    assert res["count"] == 0
    assert res["positions"] == []
    print("✅ test_positions_no_db")


def test_positions_with_db(tmp_dir):
    cfg = _config(tmp_dir, with_db=True)
    res = h.get_positions(cfg)
    assert res["ok"] is True
    # qty > 0 만 (3번째는 제외)
    assert res["count"] == 2
    symbols = sorted(p["symbol"] for p in res["positions"])
    assert symbols == ["000660", "005930"]
    print("✅ test_positions_with_db")


def test_positions_db_not_found(tmp_dir):
    cfg = _config(tmp_dir, with_db=False)
    # 존재하지 않는 경로 강제
    cfg2 = ReadOnlyServerConfig(
        audit_dir=cfg.audit_dir,
        positions_db="/tmp/nonexistent_xyz_999.sqlite",
    )
    res = h.get_positions(cfg2)
    assert res["ok"] is False
    assert res["error_code"] == "DB_NOT_FOUND"
    print("✅ test_positions_db_not_found")


# ─────────────────────────────────────────────────
# Tool 3: get_pnl_snapshot
# ─────────────────────────────────────────────────

def test_pnl_snapshot(tmp_dir):
    cfg = _config(tmp_dir, with_db=True)
    res = h.get_pnl_snapshot(
        cfg,
        starting_capital_krw="10000000",
        cash_krw="500000",
    )
    assert res["ok"] is True
    # 포지션 시가 = 7,100,000 + 6,500,000 = 13,600,000
    # equity = 500,000 + 13,600,000 = 14,100,000
    # pnl = 14,100,000 - 10,000,000 = 4,100,000
    # SQLite REAL → float이므로 ".0" 접미가 있을 수 있음, 수치로 비교
    assert float(res["position_value_krw"]) == 13600000.0
    assert float(res["equity_krw"]) == 14100000.0
    assert float(res["pnl_krw"]) == 4100000.0
    print("✅ test_pnl_snapshot")


def test_pnl_invalid_amount(tmp_dir):
    cfg = _config(tmp_dir)
    res = h.get_pnl_snapshot(
        cfg, starting_capital_krw="not-a-number", cash_krw="0",
    )
    assert res["ok"] is False
    assert res["error_code"] == "INVALID_AMOUNT"
    print("✅ test_pnl_invalid_amount")


def test_pnl_negative_starting(tmp_dir):
    cfg = _config(tmp_dir)
    res = h.get_pnl_snapshot(
        cfg, starting_capital_krw="-1", cash_krw="0",
    )
    assert res["ok"] is False
    assert res["error_code"] == "INVALID_AMOUNT"
    print("✅ test_pnl_negative_starting")


# ─────────────────────────────────────────────────
# Tool 4: get_recent_fills
# ─────────────────────────────────────────────────

def test_recent_fills(tmp_dir):
    cfg = _config(tmp_dir, with_db=True)
    res = h.get_recent_fills(cfg, limit=10)
    assert res["ok"] is True
    assert res["count"] == 2
    print("✅ test_recent_fills")


def test_recent_fills_with_since(tmp_dir):
    cfg = _config(tmp_dir, with_db=True)
    # F1 이후만
    res = h.get_recent_fills(
        cfg, limit=10,
        since_iso="2026-05-07T01:30:00+00:00",
    )
    assert res["ok"] is True
    assert res["count"] == 1  # F2만
    print("✅ test_recent_fills_with_since")


def test_recent_fills_invalid_limit(tmp_dir):
    cfg = _config(tmp_dir, with_db=True)
    res = h.get_recent_fills(cfg, limit=999999)  # 한도 초과
    assert res["ok"] is False
    assert res["error_code"] == "VALIDATION_ERROR"
    print("✅ test_recent_fills_invalid_limit")


# ─────────────────────────────────────────────────
# Tool 5: get_rejection_summary
# ─────────────────────────────────────────────────

def test_rejection_summary(tmp_dir):
    cfg = _config(tmp_dir, with_risk=True)
    res = h.get_rejection_summary(cfg)
    assert res["ok"] is True
    assert res["total_decisions"] == 4
    assert res["rejections"] == 3
    assert res["approvals"] == 1
    assert res["by_reason"]["system_paused"] == 2
    assert res["by_gate"]["kill_switch"] == 2
    print("✅ test_rejection_summary")


def test_rejection_summary_with_since(tmp_dir):
    cfg = _config(tmp_dir, with_risk=True)
    res = h.get_rejection_summary(
        cfg, since_iso="2026-05-07T03:00:00+00:00",
    )
    assert res["ok"] is True
    # 03:00 이후 — 03:00, 04:00 두 개의 rejection
    assert res["rejections"] == 2
    print("✅ test_rejection_summary_with_since")


# ─────────────────────────────────────────────────
# Tool 6: get_portfolio_risk
# ─────────────────────────────────────────────────

def test_portfolio_risk(tmp_dir):
    cfg = _config(tmp_dir, with_db=True)
    res = h.get_portfolio_risk(
        cfg,
        sector_map={"005930": "tech", "000660": "tech"},
        cash_krw="1000000",
    )
    assert res["ok"] is True
    snap = res["snapshot"]
    assert int(float(snap["total_exposure_krw"])) == 13600000
    # tech 100% — 한도 초과 경고
    assert any("tech" in w.lower() for w in snap["warnings"])
    print("✅ test_portfolio_risk")


def test_portfolio_risk_invalid_sector_map(tmp_dir):
    cfg = _config(tmp_dir, with_db=True)
    res = h.get_portfolio_risk(
        cfg,
        sector_map={"with space": "tech"},
        cash_krw="0",
    )
    assert res["ok"] is False
    assert res["error_code"] == "VALIDATION_ERROR"
    print("✅ test_portfolio_risk_invalid_sector_map")


# ─────────────────────────────────────────────────
# Tool 7: get_strategy_registry
# ─────────────────────────────────────────────────

def test_strategy_registry(tmp_dir):
    cfg = _config(tmp_dir, with_registry=True)
    res = h.get_strategy_registry(cfg)
    assert res["ok"] is True
    summary = res["registry"]
    assert summary["total_strategies"] == 1
    assert summary["active_count"] == 1
    print("✅ test_strategy_registry")


def test_strategy_registry_no_path(tmp_dir):
    cfg = _config(tmp_dir, with_registry=False)
    res = h.get_strategy_registry(cfg)
    assert res["ok"] is True
    assert "미설정" in res.get("note", "")
    print("✅ test_strategy_registry_no_path")


# ─────────────────────────────────────────────────
# Tool 8: get_trace
# ─────────────────────────────────────────────────

def test_get_trace_not_found(tmp_dir):
    cfg = _config(tmp_dir)
    res = h.get_trace(cfg, trace_id="trc-20991231-deadbeef")
    assert res["ok"] is True
    assert res["event_count"] == 0
    print("✅ test_get_trace_not_found")


def test_get_trace_invalid_id(tmp_dir):
    cfg = _config(tmp_dir)
    res = h.get_trace(cfg, trace_id="not-valid-format")
    assert res["ok"] is False
    assert res["error_code"] == "VALIDATION_ERROR"
    print("✅ test_get_trace_invalid_id")


def test_get_trace_with_data(tmp_dir):
    """real audit log 데이터 생성 후 조회."""
    from src.observability import (
        AuditWriter, TraceContext, ORIGIN_OPERATOR,
    )
    cfg = _config(tmp_dir)
    writer = AuditWriter(audit_dir=cfg.audit_dir)
    ctx = TraceContext.new(origin=ORIGIN_OPERATOR, session_id="s")
    writer.write_signal(ctx, payload={"x": 1})
    writer.write_risk(ctx.child_span("risk"), payload={"decision": "approve"})

    res = h.get_trace(cfg, trace_id=ctx.trace_id, include_tree=True)
    assert res["ok"] is True
    assert res["event_count"] == 2
    assert res["tree"] is not None
    assert res["summary"] is not None
    print("✅ test_get_trace_with_data")


def test_get_trace_disabled(tmp_dir):
    cfg = ReadOnlyServerConfig(
        audit_dir=str(tmp_dir / "audit"),
        enable_get_trace=False,
    )
    (tmp_dir / "audit").mkdir(exist_ok=True)
    res = h.get_trace(cfg, trace_id="trc-20260507-deadbeef")
    assert res["ok"] is False
    assert res["error_code"] == "DISABLED"
    print("✅ test_get_trace_disabled")


# ─────────────────────────────────────────────────
# 실행
# ─────────────────────────────────────────────────

try:
    import pytest

    @pytest.fixture
    def tmp_dir(tmp_path):
        return tmp_path
except ImportError:
    pass


def _run_all() -> int:
    failed = 0
    tests = [
        test_market_status,
        test_positions_no_db, test_positions_with_db, test_positions_db_not_found,
        test_pnl_snapshot, test_pnl_invalid_amount, test_pnl_negative_starting,
        test_recent_fills, test_recent_fills_with_since, test_recent_fills_invalid_limit,
        test_rejection_summary, test_rejection_summary_with_since,
        test_portfolio_risk, test_portfolio_risk_invalid_sector_map,
        test_strategy_registry, test_strategy_registry_no_path,
        test_get_trace_not_found, test_get_trace_invalid_id,
        test_get_trace_with_data, test_get_trace_disabled,
    ]
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        for fn in tests:
            sub = td_path / fn.__name__
            sub.mkdir()
            try:
                fn(sub)
            except AssertionError as e:
                print(f"❌ {fn.__name__}: {e}")
                failed += 1
            except Exception as e:  # noqa: BLE001
                print(f"💥 {fn.__name__}: {type(e).__name__}: {e}")
                failed += 1
    return failed


if __name__ == "__main__":
    print("─" * 50)
    print("Task 34 v0.1 — _tool_handlers 스모크 테스트")
    print("─" * 50)
    failed = _run_all()
    print("─" * 50)
    if failed == 0:
        print("✅ 모든 테스트 통과")
        sys.exit(0)
    else:
        print(f"❌ {failed}개 실패")
        sys.exit(1)
