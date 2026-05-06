"""스모크 테스트 (Smoke Test) — Task 49 v0.1 Daily Report Generator."""

import json
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.data.ohlcv_schema import OHLCVBar, Timeframe
from src.data.ohlcv_store import OHLCVStore
from src.data.symbol_master import SymbolMaster
from src.execution.fills import Fill, FillSide
from src.execution.fill_store import FillStore
from src.pnl.pnl_engine import PnLEngine
from src.pnl.position_ledger import PositionLedger
from src.pnl.position_store import PositionStore
from src.pnl.slippage import SlippageAnalyzer
from src.reports import (
    DailyReport, DailyReportBuilder, DailyReportInputs,
    aggregate_approval_audit, aggregate_execution_audit, aggregate_risk_audit,
    recommend_next_capacity,
)
from src.risk.portfolio_risk import PortfolioRiskAnalyzer

CSV_PATH = Path(__file__).parent / "data" / "reference" / "symbol_master.csv"


def _make_fill(*, fill_id, side, qty, price, fee_krw="0", tax_krw="0",
               symbol="005930", filled_at=None, broker_order_no=None):
    return Fill(
        fill_id=fill_id,
        broker_order_no=broker_order_no or f"ORD-{fill_id}",
        client_order_id=f"exec-{fill_id}",
        symbol=symbol, side=side, quantity=qty,
        price=Decimal(price),
        fee_krw=Decimal(fee_krw),
        tax_krw=Decimal(tax_krw),
        filled_at_utc=filled_at or datetime(2026, 5, 6, 9, tzinfo=timezone.utc),
        received_at_utc=datetime.now(timezone.utc),
        source="test",
    )


def _make_ohlcv_bar(symbol, close, *, time=None):
    t = time or datetime(2026, 5, 6, tzinfo=timezone.utc)
    p = Decimal(close)
    return OHLCVBar(
        symbol=symbol, timeframe=Timeframe.D1,
        bar_time_utc=t, open=p, high=p, low=p, close=p,
        volume=10000, source="test",
    )


def _build_pnl_engine_with_data():
    """일부 fill + 시세를 가진 PnLEngine."""
    pos_db = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False).name
    ohlcv_db = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False).name

    ledger = PositionLedger(PositionStore(pos_db))
    # 매수 후 일부 매도
    ledger.apply_fill(_make_fill(
        fill_id="F1", side=FillSide.BUY, qty=10, price="70000", fee_krw="100",
        filled_at=datetime(2026, 5, 6, 9, tzinfo=timezone.utc),
    ))
    ledger.apply_fill(_make_fill(
        fill_id="F2", side=FillSide.SELL, qty=5, price="75000",
        fee_krw="50", tax_krw="500",
        filled_at=datetime(2026, 5, 6, 10, tzinfo=timezone.utc),
    ))
    # 시세 76000 → 미실현 (76000 - 70010) * 5 = 29950
    ohlcv = OHLCVStore(ohlcv_db)
    ohlcv.upsert_bars([_make_ohlcv_bar("005930", "76000")])

    engine = PnLEngine(ledger, ohlcv)
    return engine, [pos_db, ohlcv_db]


# ─────────────────────────────────────────────────
# Audit Aggregator 테스트
# ─────────────────────────────────────────────────

def test_risk_audit_aggregation():
    print("\n[1] Risk audit 집계")
    audit_path = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False).name
    records = [
        {"decision": "pass", "decided_at_utc": "2026-05-06T09:00:00+00:00",
         "symbol": "005930", "strategy_id": "momentum_v04"},
        {"decision": "pass", "decided_at_utc": "2026-05-06T09:01:00+00:00",
         "symbol": "005930", "strategy_id": "momentum_v04"},
        {"decision": "reject", "decided_at_utc": "2026-05-06T09:02:00+00:00",
         "symbol": "000660", "strategy_id": "momentum_v04",
         "rejected_by_gate": "exposure_per_symbol"},
        {"decision": "reject", "decided_at_utc": "2026-05-06T09:03:00+00:00",
         "symbol": "005930", "rejected_by_gate": "rate_limit"},
        {"decision": "reject", "decided_at_utc": "2026-05-06T09:04:00+00:00",
         "symbol": "035720", "rejected_by_gate": "exposure_per_symbol"},
    ]
    try:
        with open(audit_path, "w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
        stats = aggregate_risk_audit(audit_path)
        assert stats.total == 5
        assert stats.pass_count == 2
        assert stats.reject_count == 3
        assert abs(stats.rejection_rate - 0.6) < 0.01
        assert stats.by_gate_reject["exposure_per_symbol"] == 2
        assert stats.by_gate_reject["rate_limit"] == 1
        assert stats.by_symbol_reject["000660"] == 1
        print(f"   ✅ {stats.total}건, 거부율={stats.rejection_rate:.1%}, "
              f"게이트={list(stats.by_gate_reject.keys())}")
    finally:
        Path(audit_path).unlink()


def test_execution_audit_aggregation():
    print("\n[2] Execution audit 집계")
    audit_path = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False).name
    records = [
        {"execution_id": "e1", "outcome": "submitted", "is_dry_run": False,
         "started_at_utc": "2026-05-06T09:00:00+00:00", "symbol": "005930"},
        {"execution_id": "e2", "outcome": "rejected",
         "started_at_utc": "2026-05-06T09:01:00+00:00", "symbol": "005930"},
        {"execution_id": "e3", "outcome": "submitted", "is_dry_run": True,
         "started_at_utc": "2026-05-06T09:02:00+00:00", "symbol": "000660"},
        {"execution_id": "e4", "outcome": "error",
         "started_at_utc": "2026-05-06T09:03:00+00:00",
         "symbol": "005930", "error": "broker timeout", "stage": "submit"},
    ]
    try:
        with open(audit_path, "w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
        stats = aggregate_execution_audit(audit_path)
        assert stats.total == 4
        assert stats.submitted_count == 2
        assert stats.rejected_count == 1
        assert stats.error_count == 1
        assert stats.dry_run_count == 1
        assert len(stats.error_records) == 1
        assert stats.error_records[0]["error"] == "broker timeout"
        print(f"   ✅ submitted={stats.submitted_count}, error={stats.error_count}")
    finally:
        Path(audit_path).unlink()


def test_approval_audit_aggregation():
    print("\n[3] Approval audit 집계")
    audit_path = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False).name
    records = [
        {"approved": True, "approver": "cli_human",
         "decided_at_utc": "2026-05-06T09:00:00+00:00", "response_time_sec": 5.5},
        {"approved": True, "approver": "pre_approval",
         "decided_at_utc": "2026-05-06T09:01:00+00:00", "response_time_sec": 0.001},
        {"approved": False, "approver": "cli_human",
         "decided_at_utc": "2026-05-06T09:02:00+00:00", "response_time_sec": 12.3},
    ]
    try:
        with open(audit_path, "w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
        stats = aggregate_approval_audit(audit_path)
        assert stats.total == 3
        assert stats.approved_count == 2
        assert stats.rejected_count == 1
        assert stats.by_approver["cli_human"] == 2
        assert stats.by_approver["pre_approval"] == 1
        assert stats.avg_response_time_sec is not None
        print(f"   ✅ approved={stats.approved_count}, avg_rt={stats.avg_response_time_sec:.2f}s")
    finally:
        Path(audit_path).unlink()


def test_audit_time_filter():
    print("\n[4] Audit 시간 필터")
    audit_path = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False).name
    try:
        records = [
            {"decision": "pass", "decided_at_utc": "2026-05-05T09:00:00+00:00"},
            {"decision": "pass", "decided_at_utc": "2026-05-06T09:00:00+00:00"},
            {"decision": "pass", "decided_at_utc": "2026-05-07T09:00:00+00:00"},
        ]
        with open(audit_path, "w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
        stats = aggregate_risk_audit(
            audit_path,
            since_utc=datetime(2026, 5, 6, tzinfo=timezone.utc),
            until_utc=datetime(2026, 5, 6, 23, 59, tzinfo=timezone.utc),
        )
        assert stats.total == 1
        print(f"   ✅ 시간 필터로 {stats.total}건만")
    finally:
        Path(audit_path).unlink()


def test_audit_corrupt_jsonl_skip():
    print("\n[5] 깨진 JSONL 스킵")
    audit_path = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False).name
    try:
        with open(audit_path, "w") as f:
            f.write('{"decision": "pass", "decided_at_utc": "2026-05-06T09:00:00+00:00"}\n')
            f.write('not valid json\n')
            f.write('{"decision": "reject", "decided_at_utc": "2026-05-06T09:01:00+00:00"}\n')
        stats = aggregate_risk_audit(audit_path)
        assert stats.total == 2  # 깨진 라인은 skip
        print(f"   ✅ 깨진 라인 skip, {stats.total}건만 집계")
    finally:
        Path(audit_path).unlink()


def test_audit_missing_file():
    print("\n[6] Audit 파일 없으면 빈 stats")
    stats = aggregate_risk_audit("/nonexistent/path.jsonl")
    assert stats.total == 0
    print(f"   ✅ 없는 파일 → 빈 stats")


# ─────────────────────────────────────────────────
# Capacity Recommender 테스트
# ─────────────────────────────────────────────────

def test_capacity_no_signals():
    print("\n[7] Capacity — 신호 없음 → 100%")
    rec = recommend_next_capacity(
        starting_capital_krw=Decimal("10000000"),
        realized_pnl_krw=Decimal("100000"),
        rejected_orders_count=5,
        rejection_rate=0.05,
        exceptions_count=0,
    )
    assert rec.recommend_pct == Decimal("1.00")
    assert rec.severity == "ok"
    assert rec.risk_signals == 0
    assert rec.recommended_capacity_krw == Decimal("10100000")
    print(f"   ✅ ok, 100%, 권장 {rec.recommended_capacity_krw}")


def test_capacity_one_signal():
    print("\n[8] Capacity — 1개 신호 → 90%")
    rec = recommend_next_capacity(
        starting_capital_krw=Decimal("10000000"),
        realized_pnl_krw=Decimal("0"),
        rejected_orders_count=0, rejection_rate=0.0,
        exceptions_count=1,  # 1개
    )
    assert rec.recommend_pct == Decimal("0.90")
    assert rec.severity == "low"
    assert rec.risk_signals == 1
    print(f"   ✅ low, 90%")


def test_capacity_two_signals():
    print("\n[9] Capacity — 2개 신호 → 75%")
    rec = recommend_next_capacity(
        starting_capital_krw=Decimal("10000000"),
        realized_pnl_krw=Decimal("0"),
        rejected_orders_count=100, rejection_rate=0.05,  # 횟수 1
        exceptions_count=1,                              # 예외 1
    )
    assert rec.recommend_pct == Decimal("0.75")
    assert rec.severity == "moderate"
    assert rec.risk_signals == 2
    print(f"   ✅ moderate, 75%, 신호 {rec.risk_signals}")


def test_capacity_three_signals():
    print("\n[10] Capacity — 3개 신호 → 60%")
    rec = recommend_next_capacity(
        starting_capital_krw=Decimal("10000000"),
        realized_pnl_krw=Decimal("0"),
        rejected_orders_count=100, rejection_rate=0.05,  # 횟수 1
        exceptions_count=1,                              # 예외 1
        portfolio_risk_warnings=2,                       # 포트폴리오 1
    )
    assert rec.recommend_pct == Decimal("0.60")
    assert rec.severity == "high"
    print(f"   ✅ high, 60%, 신호 {rec.risk_signals}")


def test_capacity_critical():
    print("\n[11] Capacity — 4+ 신호 (recon major +2) → 50%")
    rec = recommend_next_capacity(
        starting_capital_krw=Decimal("10000000"),
        realized_pnl_krw=Decimal("0"),
        rejected_orders_count=100, rejection_rate=0.5,  # 횟수 1 + 비율 1
        exceptions_count=1,                              # 1
        reconciliation_severity="major",                 # +2
        portfolio_risk_warnings=1,                       # 1
    )
    # 1 + 1 + 1 + 2 + 1 = 6 → critical
    assert rec.recommend_pct == Decimal("0.50")
    assert rec.severity == "critical"
    assert rec.risk_signals >= 4
    print(f"   ✅ critical, 50%, 신호 {rec.risk_signals}")


def test_capacity_negative_pnl_capped():
    print("\n[12] Capacity — 손실로 자본 음수 → 0으로 클램프")
    rec = recommend_next_capacity(
        starting_capital_krw=Decimal("100000"),
        realized_pnl_krw=Decimal("-200000"),  # 자본보다 큰 손실
        rejected_orders_count=0, rejection_rate=0.0,
        exceptions_count=0,
    )
    # current_capital = max(0, 100000 - 200000) = 0
    assert rec.current_capital_krw == Decimal("0")
    assert rec.recommended_capacity_krw == Decimal("0")
    print(f"   ✅ 음수 자본 → 0으로 클램프")


def test_capacity_unknown_recon_signal():
    print("\n[13] Capacity — recon unknown → +1 신호")
    rec = recommend_next_capacity(
        starting_capital_krw=Decimal("10000000"),
        realized_pnl_krw=Decimal("0"),
        rejected_orders_count=0, rejection_rate=0.0,
        exceptions_count=0,
        reconciliation_severity="unknown",
    )
    assert rec.risk_signals == 1
    assert "unknown" in str(rec.risk_signal_breakdown)
    print(f"   ✅ unknown → 1 신호")


def test_capacity_invalid_inputs():
    print("\n[14] Capacity — 잘못된 입력 거부")
    try:
        recommend_next_capacity(
            starting_capital_krw=Decimal("-100"),
            realized_pnl_krw=Decimal("0"),
            rejected_orders_count=0, rejection_rate=0.0,
            exceptions_count=0,
        )
        assert False
    except ValueError:
        print(f"   ✅ 음수 starting_capital 거부")

    try:
        recommend_next_capacity(
            starting_capital_krw=Decimal("100"),
            realized_pnl_krw=Decimal("0"),
            rejected_orders_count=0, rejection_rate=1.5,  # > 1
            exceptions_count=0,
        )
        assert False
    except ValueError:
        print(f"   ✅ rejection_rate > 1 거부")


# ─────────────────────────────────────────────────
# DailyReport 출력 포맷 테스트
# ─────────────────────────────────────────────────

def test_report_to_json():
    print("\n[15] DailyReport.to_json")
    rep = DailyReport(
        metadata={"session_id": "test", "session_date_kst": "2026-05-06"},
        output_1_starting_capital={"starting_capital_krw": "10000000"},
        output_2_ending_capital={"ending_capital_krw": "10500000"},
    )
    s = rep.to_json()
    parsed = json.loads(s)
    assert parsed["metadata"]["session_id"] == "test"
    assert parsed["output_1_starting_capital"]["starting_capital_krw"] == "10000000"
    print(f"   ✅ JSON 직렬화 OK")


def test_report_to_markdown():
    print("\n[16] DailyReport.to_markdown")
    rep = DailyReport(
        metadata={
            "session_id": "test-2026-05-06",
            "session_date_kst": "2026-05-06",
            "session_start_utc": "2026-05-06T00:00:00+00:00",
            "session_end_utc": "2026-05-06T06:30:00+00:00",
            "generated_at_utc": "2026-05-06T07:00:00+00:00",
            "report_version": "0.1",
        },
        output_1_starting_capital={"starting_capital_krw": "10000000", "starting_cash_krw": "10000000"},
        output_2_ending_capital={
            "ending_capital_krw": "10500000",
            "cash_krw": "9000000",
            "total_market_value_krw": "1500000",
            "return_pct": "0.05",
        },
        output_3_realized_pnl={"total_realized_pnl_krw": "300000"},
        output_4_unrealized_pnl={"total_unrealized_pnl_krw": "200000", "stale_symbols": []},
        output_5_fees_slippage={
            "total_fees_krw": "1000", "total_taxes_krw": "500",
            "slippage": {"count": 5, "avg_slippage_bps": "10.5",
                         "median_slippage_bps": "10.0", "p95_slippage_bps": "30.0",
                         "unfavorable_pct": "0.4", "partial_fill_count": 1},
        },
        output_6_strategy_attribution=[{
            "strategy_id": "momentum_v04",
            "realized_pnl_krw": "300000",
            "unrealized_pnl_krw": "200000",
            "fills_count": 5, "symbols": ["005930", "000660"],
        }],
        output_7_symbol_attribution={
            "005930": {"quantity": 10, "avg_cost_krw": "70000",
                       "current_price_krw": "75000",
                       "realized_krw": "0", "unrealized_krw": "50000"},
        },
        output_8_rejected_orders={
            "total": 10, "pass_count": 8, "reject_count": 2, "rejection_rate": 0.2,
            "by_gate_reject": {"exposure_per_symbol": 1, "rate_limit": 1},
        },
        output_10_reconciliation_status={"performed": True, "severity": "ok",
                                          "match_count": 3, "mismatch_count": 0,
                                          "by_type": {}},
        output_11_exceptions=[],
        output_12_next_session_capacity={
            "current_capital_krw": "10300000",
            "recommend_pct": "1.00",
            "recommended_capacity_krw": "10300000",
            "risk_signals": 0, "severity": "ok",
            "reasoning": ["정상 — 위험 신호 없음"],
        },
    )
    md = rep.to_markdown()
    assert "# 일일 트레이딩 리포트" in md
    assert "1. 시작 자본" in md
    assert "12. 다음 세션 자본 추천" in md
    assert "10,000,000" in md  # KRW 콤마 포맷
    assert "momentum_v04" in md
    assert "005930" in md
    print(f"   ✅ Markdown 생성 ({len(md)}자), 12개 섹션 포함")


def test_report_to_html():
    print("\n[17] DailyReport.to_html")
    rep = DailyReport(
        metadata={"session_id": "test", "session_date_kst": "2026-05-06",
                  "generated_at_utc": "2026-05-06T07:00:00+00:00"},
        output_1_starting_capital={"starting_capital_krw": "10000000"},
        output_2_ending_capital={"ending_capital_krw": "10500000"},
        output_12_next_session_capacity={
            "current_capital_krw": "10000000", "recommend_pct": "1.00",
            "recommended_capacity_krw": "10000000",
            "risk_signals": 0, "severity": "ok",
        },
    )
    html_out = rep.to_html()
    assert "<!DOCTYPE html>" in html_out
    assert "<title>" in html_out
    assert "10,000,000" in html_out
    # XSS 방어 검증 — escape 동작
    rep2 = DailyReport(
        metadata={"session_id": "<script>alert('xss')</script>",
                  "session_date_kst": "2026-05-06",
                  "generated_at_utc": "2026-05-06T07:00:00+00:00"},
    )
    html_out2 = rep2.to_html()
    assert "<script>" not in html_out2.replace("&lt;script&gt;", "")
    assert "&lt;script&gt;" in html_out2 or "&amp;lt;" in html_out2  # escape됨
    print(f"   ✅ HTML 생성 ({len(html_out)}자), XSS escape 동작")


def test_report_save_files():
    print("\n[18] save_json/markdown/html — 파일 저장")
    rep = DailyReport(
        metadata={"session_id": "test", "session_date_kst": "2026-05-06",
                  "generated_at_utc": "2026-05-06T07:00:00+00:00"},
        output_12_next_session_capacity={
            "current_capital_krw": "10000000",
            "recommend_pct": "1.00",
            "recommended_capacity_krw": "10000000",
            "risk_signals": 0, "severity": "ok",
        },
    )
    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        json_p = rep.save_json(tmpd / "r.json")
        md_p = rep.save_markdown(tmpd / "r.md")
        html_p = rep.save_html(tmpd / "r.html")
        assert json_p.exists() and json_p.stat().st_size > 0
        assert md_p.exists() and md_p.stat().st_size > 0
        assert html_p.exists() and html_p.stat().st_size > 0
        # JSON parse 가능
        data = json.loads(json_p.read_text())
        assert "metadata" in data
        print(f"   ✅ 3개 파일 저장 + JSON 파싱 OK")


# ─────────────────────────────────────────────────
# DailyReportBuilder 통합 테스트
# ─────────────────────────────────────────────────

def test_builder_no_dependencies():
    print("\n[19] Builder — 의존성 모두 None (graceful degradation)")
    inputs = DailyReportInputs(
        session_id="test-empty",
        session_date_kst=date(2026, 5, 6),
        starting_capital_krw=Decimal("10000000"),
        cash_krw=Decimal("10000000"),
        session_start_utc=datetime(2026, 5, 6, 0, tzinfo=timezone.utc),
        session_end_utc=datetime(2026, 5, 6, 6, 30, tzinfo=timezone.utc),
    )
    builder = DailyReportBuilder()
    report = builder.build(inputs)
    # 기본 항목이 모두 채워짐 (N/A or 0)
    assert report.metadata["session_id"] == "test-empty"
    assert report.output_1_starting_capital["starting_capital_krw"] == "10000000"
    # 의존 부재 → 그래도 #12는 capacity recommender에서 생성됨
    assert report.output_12_next_session_capacity["risk_signals"] >= 0
    # 예외 없음 (graceful — 의존 부재 자체는 예외 아님)
    print(f"   ✅ 의존 모두 None → graceful, 예외 {len(report.output_11_exceptions)}건")


def test_builder_with_pnl_engine():
    print("\n[20] Builder — PnLEngine 포함")
    engine, db_paths = _build_pnl_engine_with_data()
    try:
        inputs = DailyReportInputs(
            session_id="test-pnl",
            session_date_kst=date(2026, 5, 6),
            starting_capital_krw=Decimal("10000000"),
            cash_krw=Decimal("9000000"),
            session_start_utc=datetime(2026, 5, 6, 0, tzinfo=timezone.utc),
            session_end_utc=datetime(2026, 5, 6, 12, tzinfo=timezone.utc),
            pnl_engine=engine,
        )
        builder = DailyReportBuilder()
        report = builder.build(inputs)
        # 실현 P&L 검증 (F2 매도 5*5000 - 50 - 500 = 24450)
        # F1 fee 100이 첫 매수 cost에 포함되어 avg = 70010
        # 실현 = 5 * (75000 - 70010) - 50 - 500 = 24400
        realized = report.output_3_realized_pnl["total_realized_pnl_krw"]
        # 실제 계산값
        # avg = (10*70000 + 100) / 10 = 70010
        # realized_delta = 5*75000 - 5*70010 - 50 - 500 = 375000 - 350050 - 550 = 24400
        assert Decimal(realized) == Decimal("24400")
        # 미실현 = (76000 - 70010) * 5 = 29950
        unrealized = report.output_4_unrealized_pnl["total_unrealized_pnl_krw"]
        assert Decimal(unrealized) == Decimal("29950")
        print(f"   ✅ realized={realized}, unrealized={unrealized}")
    finally:
        for p in db_paths:
            Path(p).unlink()


def test_builder_with_audit_logs():
    print("\n[21] Builder — audit log 포함")
    risk_audit = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False).name
    exec_audit = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False).name
    try:
        with open(risk_audit, "w") as f:
            f.write(json.dumps({
                "decision": "pass",
                "decided_at_utc": "2026-05-06T03:00:00+00:00",
                "symbol": "005930",
            }) + "\n")
            f.write(json.dumps({
                "decision": "reject",
                "decided_at_utc": "2026-05-06T03:01:00+00:00",
                "symbol": "005930",
                "rejected_by_gate": "rate_limit",
            }) + "\n")
        with open(exec_audit, "w") as f:
            f.write(json.dumps({
                "execution_id": "e1", "outcome": "submitted",
                "started_at_utc": "2026-05-06T03:00:00+00:00",
                "symbol": "005930",
            }) + "\n")

        inputs = DailyReportInputs(
            session_id="test-audit",
            session_date_kst=date(2026, 5, 6),
            starting_capital_krw=Decimal("10000000"),
            cash_krw=Decimal("10000000"),
            session_start_utc=datetime(2026, 5, 6, 0, tzinfo=timezone.utc),
            session_end_utc=datetime(2026, 5, 6, 23, tzinfo=timezone.utc),
            risk_audit_path=Path(risk_audit),
            execution_audit_path=Path(exec_audit),
        )
        builder = DailyReportBuilder()
        report = builder.build(inputs)
        # #8 거부 데이터 확인
        out8 = report.output_8_rejected_orders
        assert out8["total"] == 2
        assert out8["reject_count"] == 1
        assert out8["pass_count"] == 1
        assert "rate_limit" in out8["by_gate_reject"]
        print(f"   ✅ #8 거부: {out8['reject_count']}/{out8['total']}, 게이트={list(out8['by_gate_reject'].keys())}")
    finally:
        Path(risk_audit).unlink()
        Path(exec_audit).unlink()


def test_builder_with_exception_in_audit():
    print("\n[22] Builder — execution audit에 error → output #11 포함")
    exec_audit = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False).name
    try:
        with open(exec_audit, "w") as f:
            f.write(json.dumps({
                "execution_id": "e1", "outcome": "error",
                "started_at_utc": "2026-05-06T03:00:00+00:00",
                "symbol": "005930", "error": "broker timeout", "stage": "submit",
            }) + "\n")
        inputs = DailyReportInputs(
            session_id="test-exc",
            session_date_kst=date(2026, 5, 6),
            starting_capital_krw=Decimal("10000000"),
            cash_krw=Decimal("10000000"),
            session_start_utc=datetime(2026, 5, 6, 0, tzinfo=timezone.utc),
            session_end_utc=datetime(2026, 5, 6, 23, tzinfo=timezone.utc),
            execution_audit_path=Path(exec_audit),
        )
        builder = DailyReportBuilder()
        report = builder.build(inputs)
        # #11 예외에 broker timeout 포함
        assert len(report.output_11_exceptions) >= 1
        msgs = [e.get("message", "") for e in report.output_11_exceptions]
        assert any("broker timeout" in m for m in msgs)
        # #12 capacity 자동 신호 — exception 1
        cap = report.output_12_next_session_capacity
        assert cap["risk_signals"] >= 1
        print(f"   ✅ #11 예외 {len(report.output_11_exceptions)}건, "
              f"#12 신호 {cap['risk_signals']}")
    finally:
        Path(exec_audit).unlink()


def test_builder_save_all_formats():
    print("\n[23] Builder.build_and_save — 3 포맷 저장")
    inputs = DailyReportInputs(
        session_id="test-save",
        session_date_kst=date(2026, 5, 6),
        starting_capital_krw=Decimal("10000000"),
        cash_krw=Decimal("10000000"),
        session_start_utc=datetime(2026, 5, 6, 0, tzinfo=timezone.utc),
        session_end_utc=datetime(2026, 5, 6, 23, tzinfo=timezone.utc),
    )
    builder = DailyReportBuilder()
    with tempfile.TemporaryDirectory() as tmp:
        paths = builder.build_and_save(
            inputs, tmp,
            formats=("json", "md", "html"),
        )
        assert "json" in paths and paths["json"].exists()
        assert "md" in paths and paths["md"].exists()
        assert "html" in paths and paths["html"].exists()
        # 내용 검증
        data = json.loads(paths["json"].read_text())
        assert data["metadata"]["session_id"] == "test-save"
        md_content = paths["md"].read_text()
        assert "test-save" in md_content
        html_content = paths["html"].read_text()
        assert "<!DOCTYPE html>" in html_content
        print(f"   ✅ 3개 포맷 저장: {[p.name for p in paths.values()]}")


def test_builder_full_integration():
    print("\n[24] Builder — 전체 통합 (PnL + audit + portfolio risk)")
    engine, db_paths = _build_pnl_engine_with_data()
    risk_audit = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False).name
    fills_db = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False).name
    try:
        with open(risk_audit, "w") as f:
            f.write(json.dumps({
                "decision": "pass",
                "decided_at_utc": "2026-05-06T03:00:00+00:00",
                "symbol": "005930",
            }) + "\n")
            f.write(json.dumps({
                "decision": "reject",
                "decided_at_utc": "2026-05-06T03:01:00+00:00",
                "symbol": "005930",
                "rejected_by_gate": "exposure_per_symbol",
            }) + "\n")

        sm = SymbolMaster.from_csv(CSV_PATH)
        portfolio_risk = PortfolioRiskAnalyzer(sm)
        slippage = SlippageAnalyzer(FillStore(fills_db))

        inputs = DailyReportInputs(
            session_id="test-full-2026-05-06",
            session_date_kst=date(2026, 5, 6),
            starting_capital_krw=Decimal("10000000"),
            cash_krw=Decimal("9000000"),
            session_start_utc=datetime(2026, 5, 6, 0, tzinfo=timezone.utc),
            session_end_utc=datetime(2026, 5, 6, 23, tzinfo=timezone.utc),
            pnl_engine=engine,
            slippage_analyzer=slippage,
            portfolio_risk_analyzer=portfolio_risk,
            risk_audit_path=Path(risk_audit),
        )
        builder = DailyReportBuilder()
        report = builder.build(inputs)

        # 모든 12개 항목이 채워짐
        assert report.output_1_starting_capital["starting_capital_krw"] == "10000000"
        assert report.output_2_ending_capital["ending_capital_krw"] != "N/A"
        assert Decimal(report.output_3_realized_pnl["total_realized_pnl_krw"]) > 0
        assert Decimal(report.output_4_unrealized_pnl["total_unrealized_pnl_krw"]) > 0
        assert "slippage" in report.output_5_fees_slippage
        assert len(report.output_6_strategy_attribution) >= 1
        assert "005930" in report.output_7_symbol_attribution
        assert report.output_8_rejected_orders["reject_count"] == 1
        assert "portfolio" in report.output_9_risk_limit_usage
        assert report.output_10_reconciliation_status["performed"] is False
        assert isinstance(report.output_11_exceptions, list)
        assert report.output_12_next_session_capacity["risk_signals"] >= 1  # recon unknown +1

        # JSON 직렬화 가능
        s = report.to_json()
        json.loads(s)
        # Markdown / HTML 정상
        md = report.to_markdown()
        html_out = report.to_html()
        assert len(md) > 500
        assert len(html_out) > 1000
        print(f"   ✅ 모든 12개 항목 채워짐, 3 포맷 정상")
    finally:
        for p in db_paths:
            Path(p).unlink()
        Path(risk_audit).unlink()
        Path(fills_db).unlink()


# ─────────────────────────────────────────────────
# 보안 / 비밀 누출 테스트
# ─────────────────────────────────────────────────

def test_audit_secret_keywords_filtered():
    print("\n[25] Audit log secret 키 마스킹")
    # 비밀이 audit에 들어가도 집계 시 표시되지 않아야 함
    audit_path = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False).name
    try:
        with open(audit_path, "w") as f:
            f.write(json.dumps({
                "decision": "pass",
                "decided_at_utc": "2026-05-06T09:00:00+00:00",
                "app_secret": "BAD_SECRET_VALUE",  # 키워드 포함
                "auth_token": "BAD_TOKEN_VALUE",
                "account_no": "12345678-01",
                "symbol": "005930",
            }) + "\n")
        # aggregate_risk_audit는 이 필드들을 사용하지 않으므로 자동 제외
        stats = aggregate_risk_audit(audit_path)
        # 비밀 값들이 stats에 포함되지 않아야 함
        d = stats.to_dict()
        s = json.dumps(d)
        assert "BAD_SECRET_VALUE" not in s
        assert "BAD_TOKEN_VALUE" not in s
        assert "12345678-01" not in s
        print(f"   ✅ stats dict에 비밀 누출 없음")
    finally:
        Path(audit_path).unlink()


def test_report_no_secrets_in_output():
    print("\n[26] DailyReport 출력에 비밀 없음")
    rep = DailyReport(
        metadata={"session_id": "test", "session_date_kst": "2026-05-06",
                  "generated_at_utc": "2026-05-06T07:00:00+00:00"},
    )
    j = rep.to_json()
    md = rep.to_markdown()
    html_out = rep.to_html()
    for s in (j, md, html_out):
        assert "secret" not in s.lower() or "secret_value" not in s.lower()
        assert "app_secret" not in s.lower()
        assert "BAD_TOKEN" not in s
    print(f"   ✅ 3개 출력 모두 비밀 키워드 없음")


# ─────────────────────────────────────────────────
# 최종 KRW 포맷 검증
# ─────────────────────────────────────────────────

def test_krw_formatting_in_markdown():
    print("\n[27] Markdown KRW 포맷 (콤마)")
    from src.reports.daily_report import _fmt_krw
    assert _fmt_krw("10000000") == "10,000,000 KRW"
    assert _fmt_krw("0") == "0 KRW"
    assert _fmt_krw("-50000") == "-50,000 KRW"
    print(f"   ✅ KRW 콤마 포맷 정상")


def test_pct_formatting():
    print("\n[28] Markdown 퍼센트 포맷")
    from src.reports.daily_report import _fmt_pct
    assert _fmt_pct("0.05") == "5.00%"
    assert _fmt_pct("1") == "100.00%"
    assert _fmt_pct("0", 0) == "0%"
    print(f"   ✅ 퍼센트 포맷 정상")


# ─────────────────────────────────────────────────
# Run All
# ─────────────────────────────────────────────────

if __name__ == "__main__":
    test_risk_audit_aggregation()
    test_execution_audit_aggregation()
    test_approval_audit_aggregation()
    test_audit_time_filter()
    test_audit_corrupt_jsonl_skip()
    test_audit_missing_file()
    test_capacity_no_signals()
    test_capacity_one_signal()
    test_capacity_two_signals()
    test_capacity_three_signals()
    test_capacity_critical()
    test_capacity_negative_pnl_capped()
    test_capacity_unknown_recon_signal()
    test_capacity_invalid_inputs()
    test_report_to_json()
    test_report_to_markdown()
    test_report_to_html()
    test_report_save_files()
    test_builder_no_dependencies()
    test_builder_with_pnl_engine()
    test_builder_with_audit_logs()
    test_builder_with_exception_in_audit()
    test_builder_save_all_formats()
    test_builder_full_integration()
    test_audit_secret_keywords_filtered()
    test_report_no_secrets_in_output()
    test_krw_formatting_in_markdown()
    test_pct_formatting()
    print("\n🎉 모든 스모크 테스트 통과 (All smoke tests passed)")
