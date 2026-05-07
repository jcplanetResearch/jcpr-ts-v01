"""
스모크 테스트 — daily_report (end-to-end)
==========================================

JCPR Trading System - jcpr-ts-v01
Task 49 v0.2

DailyReportBuilder 통합 테스트.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from src.reports import DailyReportBuilder, DailyReportInputs  # noqa: E402


# ─────────────────────────────────────────────────
# 픽스처 — DB + audit log
# ─────────────────────────────────────────────────

def _make_positions_db(path: str, fill_time_iso: str) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(f"""
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
                filled_at_utc TEXT,
                strategy_id TEXT,
                intended_price_krw REAL
            );
            CREATE TABLE realized_pnl (
                pnl_id TEXT PRIMARY KEY,
                symbol TEXT,
                realized_pnl_krw REAL,
                strategy_id TEXT,
                realized_at_utc TEXT
            );
            INSERT INTO positions VALUES
                ('005930', 100, 70000, 'long', '{fill_time_iso}'),
                ('035420', 50, 200000, 'long', '{fill_time_iso}');
            INSERT INTO fills VALUES
                ('f1', '005930', 'buy', 100, 70000, 7000000, 700, 0, '{fill_time_iso}', 'momentum_v1', 69900),
                ('f2', '035420', 'buy', 50, 200000, 10000000, 1000, 0, '{fill_time_iso}', 'momentum_v1', 200000);
            INSERT INTO realized_pnl VALUES
                ('rp1', '005930', 50000, 'momentum_v1', '{fill_time_iso}');
        """)


def _make_ohlcv_db(path: str) -> None:
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


# ─────────────────────────────────────────────────
# 테스트
# ─────────────────────────────────────────────────

def test_minimal_no_data():
    """아무 데이터 소스 없이 빌드 — 빈 리포트도 정상 생성되어야."""
    inputs = DailyReportInputs(
        session_id="test-empty",
        session_date_kst=date(2026, 5, 7),
        session_start_utc=datetime(2026, 5, 7, 0, tzinfo=timezone.utc),
        session_end_utc=datetime(2026, 5, 7, 23, tzinfo=timezone.utc),
        starting_capital_krw=Decimal("10000000"),
        cash_krw=Decimal("10000000"),
    )
    builder = DailyReportBuilder()
    report = builder.build(inputs)

    assert report.metadata["session_id"] == "test-empty"
    assert report.output_1_starting_capital["amount_krw"] == "10000000"
    # 데이터 없으니 종료 = 시작
    assert Decimal(report.output_2_ending_capital["total_equity_krw"]) == Decimal("10000000")
    # 거부/예외 없음
    assert report.output_8_rejected_orders["total_evaluations"] == 0
    assert report.output_11_exceptions == []
    # capacity는 hold
    assert report.output_12_next_session_capacity["stage"] == "hold"
    print("✅ test_minimal_no_data")


def test_full_session_with_db_and_audit(tmp_dir):
    """DB + audit log 모두 주입한 통합."""
    pos_db = str(tmp_dir / "positions.db")
    ohlcv_db = str(tmp_dir / "ohlcv.db")
    risk_audit = tmp_dir / "risk.jsonl"
    exec_audit = tmp_dir / "exec.jsonl"

    fill_time = "2026-05-07T01:00:00+00:00"
    _make_positions_db(pos_db, fill_time)
    _make_ohlcv_db(ohlcv_db)

    risk_audit.write_text("\n".join([
        json.dumps({"evaluated_at_utc": "2026-05-07T01:00:00+00:00",
                    "decision": "approve", "symbol": "005930"}),
        json.dumps({"evaluated_at_utc": "2026-05-07T02:00:00+00:00",
                    "decision": "reject", "rejected_gate": "g1",
                    "rejection_reason": "r1", "symbol": "035420"}),
    ]) + "\n", encoding="utf-8")

    exec_audit.write_text(
        json.dumps({"started_at_utc": "2026-05-07T01:00:00+00:00",
                    "execution_id": "e1", "outcome": "error",
                    "stage": "submit", "error": "broker timeout",
                    "symbol": "005930"}) + "\n",
        encoding="utf-8",
    )

    inputs = DailyReportInputs(
        session_id="test-full",
        session_date_kst=date(2026, 5, 7),
        session_start_utc=datetime(2026, 5, 7, 0, tzinfo=timezone.utc),
        session_end_utc=datetime(2026, 5, 7, 23, tzinfo=timezone.utc),
        starting_capital_krw=Decimal("20000000"),
        cash_krw=Decimal("3000000"),
        positions_db=pos_db,
        ohlcv_db=ohlcv_db,
        risk_audit_path=str(risk_audit),
        execution_audit_path=str(exec_audit),
    )
    builder = DailyReportBuilder()
    report = builder.build(inputs)

    # #2 종료 자본 — 현금 3M + (100*71000 + 50*202000) = 3M + 7.1M + 10.1M = 20.2M
    total_eq = Decimal(report.output_2_ending_capital["total_equity_krw"])
    assert total_eq == Decimal("20200000"), f"Got {total_eq}"

    # #3 실현 = 50000
    assert Decimal(report.output_3_realized_pnl["amount_krw"]) == Decimal("50000")

    # #4 미실현 = 100*1000 + 50*2000 = 200000
    assert Decimal(report.output_4_unrealized_pnl["amount_krw"]) == Decimal("200000")

    # #5 수수료 = 700 + 1000 = 1700
    assert Decimal(report.output_5_fees_slippage["total_fees_krw"]) == Decimal("1700")

    # #5 슬리피지 — f1: (69900-70000)*100 = -10000, f2: 0
    assert Decimal(report.output_5_fees_slippage["total_slippage_krw"]) == Decimal("-10000")

    # #6 전략 기여도
    assert len(report.output_6_strategy_attribution) == 1
    assert report.output_6_strategy_attribution[0]["strategy_id"] == "momentum_v1"

    # #7 종목 기여도
    assert len(report.output_7_symbol_attribution["positions"]) == 2

    # #8 거부 1건
    assert report.output_8_rejected_orders["total_evaluations"] == 2
    assert report.output_8_rejected_orders["rejected"] == 1
    assert report.output_8_rejected_orders["rejection_rate"] == 0.5

    # #11 예외 1건
    assert len(report.output_11_exceptions) == 1
    assert "broker timeout" in report.output_11_exceptions[0]["message"]

    # #12 추천 — 거부율 50% (critical) + 예외 1건 (warning) → reduce_strong
    cap = report.output_12_next_session_capacity
    assert cap["stage"] == "reduce_strong", f"Got {cap['stage']}"

    print("✅ test_full_session_with_db_and_audit")


def test_to_json_valid():
    """to_json — 유효한 JSON 반환."""
    inputs = DailyReportInputs(
        session_id="test-json",
        session_date_kst=date(2026, 5, 7),
        session_start_utc=datetime(2026, 5, 7, 0, tzinfo=timezone.utc),
        session_end_utc=datetime(2026, 5, 7, 23, tzinfo=timezone.utc),
        starting_capital_krw=Decimal("10000000"),
        cash_krw=Decimal("10000000"),
    )
    report = DailyReportBuilder().build(inputs)
    s = report.to_json()
    parsed = json.loads(s)
    assert parsed["metadata"]["session_id"] == "test-json"
    assert "final_outputs" in parsed
    assert "1_starting_capital" in parsed["final_outputs"]
    assert "12_next_session_capacity" in parsed["final_outputs"]
    print("✅ test_to_json_valid")


def test_to_markdown_contains_all_sections():
    """Markdown — 12개 final output 섹션 모두 포함."""
    inputs = DailyReportInputs(
        session_id="test-md",
        session_date_kst=date(2026, 5, 7),
        session_start_utc=datetime(2026, 5, 7, 0, tzinfo=timezone.utc),
        session_end_utc=datetime(2026, 5, 7, 23, tzinfo=timezone.utc),
        starting_capital_krw=Decimal("10000000"),
        cash_krw=Decimal("10000000"),
    )
    md = DailyReportBuilder().build(inputs).to_markdown()
    for n in range(1, 13):
        assert f"## #{n} " in md, f"Missing section #{n}"
    print("✅ test_to_markdown_contains_all_sections")


def test_to_html_valid_structure():
    """HTML — DOCTYPE, body, table 포함."""
    inputs = DailyReportInputs(
        session_id="test-html",
        session_date_kst=date(2026, 5, 7),
        session_start_utc=datetime(2026, 5, 7, 0, tzinfo=timezone.utc),
        session_end_utc=datetime(2026, 5, 7, 23, tzinfo=timezone.utc),
        starting_capital_krw=Decimal("10000000"),
        cash_krw=Decimal("10000000"),
    )
    html = DailyReportBuilder().build(inputs).to_html()
    assert html.startswith("<!DOCTYPE html>")
    assert "<body>" in html
    assert "@media print" in html
    print("✅ test_to_html_valid_structure")


def test_build_and_save_three_formats(tmp_dir):
    """3 포맷 모두 저장 + 파일 존재."""
    inputs = DailyReportInputs(
        session_id="test-save",
        session_date_kst=date(2026, 5, 7),
        session_start_utc=datetime(2026, 5, 7, 0, tzinfo=timezone.utc),
        session_end_utc=datetime(2026, 5, 7, 23, tzinfo=timezone.utc),
        starting_capital_krw=Decimal("10000000"),
        cash_krw=Decimal("10000000"),
    )
    out = tmp_dir / "reports"
    builder = DailyReportBuilder()
    paths = builder.build_and_save(inputs, str(out))
    assert "json" in paths
    assert "md" in paths
    assert "html" in paths
    for fmt, p in paths.items():
        assert p.exists(), f"{fmt} not created"
        size = p.stat().st_size
        assert size > 100, f"{fmt} seems empty ({size} bytes)"
    print("✅ test_build_and_save_three_formats")


def test_tz_naive_rejected():
    """tz-naive datetime 거부."""
    try:
        DailyReportInputs(
            session_id="test-tz",
            session_date_kst=date(2026, 5, 7),
            session_start_utc=datetime(2026, 5, 7, 0),  # tz-naive
            session_end_utc=datetime(2026, 5, 7, 23, tzinfo=timezone.utc),
            starting_capital_krw=Decimal("10000000"),
            cash_krw=Decimal("10000000"),
        )
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "tz-aware" in str(e)
    print("✅ test_tz_naive_rejected")


def test_dataclass_immutable():
    """frozen=True 확인."""
    inputs = DailyReportInputs(
        session_id="test-frozen",
        session_date_kst=date(2026, 5, 7),
        session_start_utc=datetime(2026, 5, 7, 0, tzinfo=timezone.utc),
        session_end_utc=datetime(2026, 5, 7, 23, tzinfo=timezone.utc),
        starting_capital_krw=Decimal("10000000"),
        cash_krw=Decimal("10000000"),
    )
    try:
        inputs.session_id = "changed"  # type: ignore[misc]
        assert False, "Should have raised FrozenInstanceError"
    except Exception:
        pass
    print("✅ test_dataclass_immutable")


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
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        # 인자 없는
        for fn in [
            test_minimal_no_data,
            test_to_json_valid,
            test_to_markdown_contains_all_sections,
            test_to_html_valid_structure,
            test_tz_naive_rejected,
            test_dataclass_immutable,
        ]:
            try:
                fn()
            except AssertionError as e:
                print(f"❌ {fn.__name__}: {e}")
                failed += 1
            except Exception as e:  # noqa: BLE001
                print(f"💥 {fn.__name__}: {type(e).__name__}: {e}")
                failed += 1
        # tmp_dir 필요
        for fn in [
            test_full_session_with_db_and_audit,
            test_build_and_save_three_formats,
        ]:
            try:
                fn(td_path)
            except AssertionError as e:
                print(f"❌ {fn.__name__}: {e}")
                failed += 1
            except Exception as e:  # noqa: BLE001
                print(f"💥 {fn.__name__}: {type(e).__name__}: {e}")
                failed += 1
    return failed


if __name__ == "__main__":
    print("─" * 50)
    print("Task 49 v0.2 — daily_report 통합 스모크 테스트")
    print("─" * 50)
    failed = _run_all()
    print("─" * 50)
    if failed == 0:
        print("✅ 모든 테스트 통과")
        sys.exit(0)
    else:
        print(f"❌ {failed}개 실패")
        sys.exit(1)
