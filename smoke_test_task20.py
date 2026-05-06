"""스모크 테스트 (Smoke Test) — Task 20 v0.1 Risk Rejection Reporting."""

import csv
import io
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))

from src.reports import (
    DEFAULT_THRESHOLDS,
    DiagnosticFinding,
    GateRejectionAnalysis,
    RejectionAnalyzer,
    RejectionReport,
    SEVERITY_CRITICAL,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    diagnose,
)

KST = ZoneInfo("Asia/Seoul")


def _write_jsonl(path: Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _make_record(
    *,
    decision: str,
    decided_at_utc: str,
    symbol: str = "005930",
    strategy: str = "momentum_v04",
    gate: str | None = None,
    reason: str | None = None,
):
    rec = {
        "decision": decision,
        "decided_at_utc": decided_at_utc,
        "symbol": symbol,
        "strategy_id": strategy,
    }
    if decision == "reject":
        rec["rejected_by_gate"] = gate or "unknown"
        if reason:
            rec["reason"] = reason
    return rec


# ─────────────────────────────────────────────────
# Diagnostics 단위 테스트
# ─────────────────────────────────────────────────

def test_diagnose_insufficient_data():
    print("\n[1] Diagnose — 데이터 부족")
    findings = diagnose(
        total_evaluations=5,
        reject_count=2,
        by_gate_reject={"rate_limit": 2},
        by_symbol_reject={"005930": 2},
        rolling_rates=[],
    )
    assert len(findings) == 1
    assert findings[0].code == "insufficient_data"
    assert findings[0].severity == SEVERITY_INFO
    print(f"   ✅ insufficient_data 진단")


def test_diagnose_kill_switch_critical():
    print("\n[2] Diagnose — kill_switch critical")
    findings = diagnose(
        total_evaluations=100,
        reject_count=5,
        by_gate_reject={"kill_switch": 5},
        by_symbol_reject={"005930": 5},
        rolling_rates=[],
    )
    crit = [f for f in findings if f.severity == SEVERITY_CRITICAL]
    assert any(f.code == "kill_switch_activated" for f in crit)
    print(f"   ✅ kill_switch → critical")


def test_diagnose_daily_loss_critical():
    print("\n[3] Diagnose — daily_loss_limit critical")
    findings = diagnose(
        total_evaluations=100,
        reject_count=3,
        by_gate_reject={"daily_loss_limit": 3},
        by_symbol_reject={"005930": 3},
        rolling_rates=[],
    )
    assert any(
        f.code == "daily_loss_limit_hit" and f.severity == SEVERITY_CRITICAL
        for f in findings
    )
    print(f"   ✅ daily_loss → critical")


def test_diagnose_high_rate_limit():
    print("\n[4] Diagnose — rate_limit > 30% → warning")
    findings = diagnose(
        total_evaluations=100,
        reject_count=20,
        by_gate_reject={"order_rate_limit": 8},  # 8/20 = 40%
        by_symbol_reject={"005930": 20},
        rolling_rates=[],
    )
    assert any(
        f.code == "high_rate_limit_rejections" and f.severity == SEVERITY_WARNING
        for f in findings
    )
    print(f"   ✅ rate_limit 40% → warning")


def test_diagnose_high_exposure():
    print("\n[5] Diagnose — exposure > 30% → warning")
    findings = diagnose(
        total_evaluations=100,
        reject_count=20,
        by_gate_reject={
            "exposure_per_symbol": 5,
            "portfolio_total_exposure": 5,
        },  # 10/20 = 50%
        by_symbol_reject={"005930": 20},
        rolling_rates=[],
    )
    assert any(
        f.code == "high_exposure_rejections" and f.severity == SEVERITY_WARNING
        for f in findings
    )
    print(f"   ✅ exposure 50% → warning")


def test_diagnose_sector_concentration():
    print("\n[6] Diagnose — sector_concentration warning")
    findings = diagnose(
        total_evaluations=100,
        reject_count=20,
        by_gate_reject={"sector_concentration": 8},  # 40%
        by_symbol_reject={"005930": 20},
        rolling_rates=[],
    )
    assert any(
        f.code == "high_sector_concentration_rejections"
        for f in findings
    )
    print(f"   ✅ sector_concentration 40% → warning")


def test_diagnose_market_state():
    print("\n[7] Diagnose — market_state >= 5 → warning")
    findings = diagnose(
        total_evaluations=100,
        reject_count=10,
        by_gate_reject={"market_state": 5},
        by_symbol_reject={"005930": 10},
        rolling_rates=[],
    )
    assert any(f.code == "market_state_rejections" for f in findings)
    print(f"   ✅ market_state 5건 → warning")


def test_diagnose_price_sanity():
    print("\n[8] Diagnose — price_sanity >= 3 → warning")
    findings = diagnose(
        total_evaluations=100,
        reject_count=10,
        by_gate_reject={"price_sanity": 3},
        by_symbol_reject={"005930": 10},
        rolling_rates=[],
    )
    assert any(f.code == "price_sanity_rejections" for f in findings)
    print(f"   ✅ price_sanity 3건 → warning")


def test_diagnose_single_symbol_dominance():
    print("\n[9] Diagnose — 단일 종목 70% 이상 → warning")
    findings = diagnose(
        total_evaluations=100,
        reject_count=10,
        by_gate_reject={"order_rate_limit": 10},
        by_symbol_reject={"005930": 8, "000660": 2},  # 005930: 80%
        rolling_rates=[],
    )
    target = next(
        (f for f in findings if f.code == "single_symbol_dominance"),
        None,
    )
    assert target is not None
    assert target.related_symbol == "005930"
    print(f"   ✅ 005930 80% → warning")


def test_diagnose_window_spike():
    print("\n[10] Diagnose — 윈도우 거부율 폭증")
    findings = diagnose(
        total_evaluations=100,
        reject_count=30,
        by_gate_reject={"order_rate_limit": 30},
        by_symbol_reject={"005930": 30},
        rolling_rates=[
            {
                "window_start_kst": "2026-05-06 09:00",
                "rate": 0.6,  # 60% > 50%
                "count": 20,
                "reject_count": 12,
            },
            {
                "window_start_kst": "2026-05-06 09:30",
                "rate": 0.1,  # 10% — 정상
                "count": 20,
                "reject_count": 2,
            },
        ],
    )
    spike = [f for f in findings if f.code == "window_rejection_spike"]
    assert len(spike) == 1  # 첫 번째만 spike
    print(f"   ✅ 1개 윈도우 폭증 감지")


def test_diagnose_no_findings():
    print("\n[11] Diagnose — 정상 → no_significant_patterns")
    # 5건 거부, rate_limit 1 (20%), duplicate 2 (40%), price 0
    # exposure 0, single_symbol 005930 2/5 = 40% (< 70%)
    # rate_limit 20% < 30%, exposure 0% < 30%
    findings = diagnose(
        total_evaluations=100,
        reject_count=5,
        by_gate_reject={"order_rate_limit": 1, "duplicate_order": 4},
        by_symbol_reject={"005930": 2, "000660": 2, "035720": 1},
        rolling_rates=[],
    )
    # 모든 임계값 미만이어야 함
    codes = {f.code for f in findings}
    assert "no_significant_patterns" in codes
    assert findings[0].severity == SEVERITY_INFO
    print(f"   ✅ 정상 → info, codes={codes}")


def test_diagnose_thresholds_override():
    print("\n[12] Diagnose — 임계값 override")
    # 기본 30%면 통과인 25%를 override로 잡기
    findings = diagnose(
        total_evaluations=100,
        reject_count=20,
        by_gate_reject={"order_rate_limit": 5},  # 25%
        by_symbol_reject={"005930": 20},
        rolling_rates=[],
        thresholds={"rate_limit_concern_pct": 0.20},  # 20% 임계
    )
    assert any(f.code == "high_rate_limit_rejections" for f in findings)
    print(f"   ✅ override → 25%도 warning")


# ─────────────────────────────────────────────────
# RejectionAnalyzer 통합 테스트
# ─────────────────────────────────────────────────

def test_analyzer_basic():
    print("\n[13] RejectionAnalyzer — 기본 집계")
    audit_path = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False).name
    try:
        records = [
            _make_record(decision="pass", decided_at_utc="2026-05-06T03:00:00+00:00"),
            _make_record(decision="pass", decided_at_utc="2026-05-06T03:01:00+00:00",
                         symbol="000660"),
            _make_record(decision="reject", decided_at_utc="2026-05-06T03:02:00+00:00",
                         gate="order_rate_limit", reason="too fast"),
            _make_record(decision="reject", decided_at_utc="2026-05-06T03:03:00+00:00",
                         symbol="000660", gate="exposure_per_symbol",
                         reason="exposure > 0.20"),
            _make_record(decision="reject", decided_at_utc="2026-05-06T03:04:00+00:00",
                         gate="order_rate_limit", reason="too fast"),
        ]
        _write_jsonl(Path(audit_path), records)

        analyzer = RejectionAnalyzer()
        report = analyzer.analyze(audit_path)
        assert report.total_evaluations == 5
        assert report.pass_count == 2
        assert report.reject_count == 3
        assert abs(report.rejection_rate - 0.6) < 0.01
        assert report.by_gate["order_rate_limit"].reject_count == 2
        assert report.by_gate["exposure_per_symbol"].reject_count == 1
        assert report.by_symbol["005930"] == 2  # 2개 거부 — rate_limit (no symbol overridden)
        # 005930 rate_limit 2건, 000660 exposure 1건
        assert report.by_symbol["000660"] == 1
        print(f"   ✅ 5건 평가, 게이트별 분포 OK")
    finally:
        Path(audit_path).unlink()


def test_analyzer_by_gate_top_symbols():
    print("\n[14] Analyzer — 게이트별 top_symbols")
    audit_path = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False).name
    try:
        records = []
        for i, sym in enumerate(["005930", "005930", "005930", "000660", "035720"]):
            records.append(_make_record(
                decision="reject",
                decided_at_utc=f"2026-05-06T03:{i:02d}:00+00:00",
                symbol=sym,
                gate="exposure_per_symbol",
            ))
        # 통과도 추가하여 데이터 부족 진단 회피
        for i in range(10):
            records.append(_make_record(
                decision="pass",
                decided_at_utc=f"2026-05-06T04:{i:02d}:00+00:00",
            ))
        _write_jsonl(Path(audit_path), records)

        analyzer = RejectionAnalyzer()
        report = analyzer.analyze(audit_path)
        gate = report.by_gate["exposure_per_symbol"]
        assert gate.reject_count == 5
        # top 종목은 005930 (3건)
        assert gate.top_symbols[0] == ("005930", 3)
        assert ("000660", 1) in gate.top_symbols
        print(f"   ✅ top_symbols: {gate.top_symbols[:3]}")
    finally:
        Path(audit_path).unlink()


def test_analyzer_by_hour_kst():
    print("\n[15] Analyzer — 시간대별 KST 분석")
    audit_path = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False).name
    try:
        # UTC 03:00 = KST 12:00
        records = [
            _make_record(decision="pass", decided_at_utc="2026-05-06T03:00:00+00:00"),
            _make_record(decision="reject", decided_at_utc="2026-05-06T03:30:00+00:00",
                         gate="rate_limit"),
            # UTC 06:00 = KST 15:00
            _make_record(decision="pass", decided_at_utc="2026-05-06T06:00:00+00:00"),
            _make_record(decision="pass", decided_at_utc="2026-05-06T06:01:00+00:00"),
        ]
        _write_jsonl(Path(audit_path), records)

        analyzer = RejectionAnalyzer()
        report = analyzer.analyze(audit_path)
        # 12시 (KST) — 1 pass, 1 reject
        assert report.by_hour_kst[12]["total"] == 2
        assert report.by_hour_kst[12]["reject_count"] == 1
        assert report.by_hour_kst[12]["pass_count"] == 1
        # 15시 (KST) — 2 pass
        assert report.by_hour_kst[15]["total"] == 2
        assert report.by_hour_kst[15]["reject_count"] == 0
        print(f"   ✅ 12시 KST: 50% 거부, 15시 KST: 0%")
    finally:
        Path(audit_path).unlink()


def test_analyzer_rolling_window():
    print("\n[16] Analyzer — 30분 윈도우 추세")
    audit_path = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False).name
    try:
        records = []
        # 첫 30분 (UTC 03:00~03:29) — 거부 다수
        for i in range(10):
            records.append(_make_record(
                decision="reject" if i < 8 else "pass",
                decided_at_utc=f"2026-05-06T03:{i:02d}:00+00:00",
                gate="rate_limit" if i < 8 else None,
            ))
        # 둘째 30분 (UTC 03:30~03:59) — 정상
        for i in range(10):
            records.append(_make_record(
                decision="pass",
                decided_at_utc=f"2026-05-06T03:{30 + i:02d}:00+00:00",
            ))
        _write_jsonl(Path(audit_path), records)

        analyzer = RejectionAnalyzer(window_minutes=30)
        report = analyzer.analyze(audit_path)
        windows = report.rolling_rejection_rates
        assert len(windows) == 2
        # 첫 윈도우 80% reject
        assert windows[0]["count"] == 10
        assert windows[0]["reject_count"] == 8
        assert windows[0]["rate"] == 0.8
        # 둘째 윈도우 0% reject
        assert windows[1]["count"] == 10
        assert windows[1]["rate"] == 0.0
        print(f"   ✅ 윈도우 1: 80%, 윈도우 2: 0%")
    finally:
        Path(audit_path).unlink()


def test_analyzer_15min_window():
    print("\n[17] Analyzer — 15분 윈도우 (커스텀)")
    audit_path = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False).name
    try:
        records = []
        for i in range(20):
            records.append(_make_record(
                decision="pass",
                decided_at_utc=f"2026-05-06T03:{i:02d}:00+00:00",
            ))
        _write_jsonl(Path(audit_path), records)

        analyzer = RejectionAnalyzer(window_minutes=15)
        report = analyzer.analyze(audit_path)
        # UTC 03:00~03:14 (15min) + 03:15~03:19 (5min) = 2 윈도우
        assert len(report.rolling_rejection_rates) == 2
        print(f"   ✅ 15분 윈도우 2개")
    finally:
        Path(audit_path).unlink()


def test_analyzer_symbol_gate_matrix():
    print("\n[18] Analyzer — 종목 × 게이트 매트릭스")
    audit_path = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False).name
    try:
        records = [
            _make_record(decision="reject", decided_at_utc="2026-05-06T03:00:00+00:00",
                         symbol="005930", gate="rate_limit"),
            _make_record(decision="reject", decided_at_utc="2026-05-06T03:01:00+00:00",
                         symbol="005930", gate="exposure_per_symbol"),
            _make_record(decision="reject", decided_at_utc="2026-05-06T03:02:00+00:00",
                         symbol="000660", gate="rate_limit"),
            _make_record(decision="reject", decided_at_utc="2026-05-06T03:03:00+00:00",
                         symbol="005930", gate="rate_limit"),
        ]
        _write_jsonl(Path(audit_path), records)

        analyzer = RejectionAnalyzer()
        report = analyzer.analyze(audit_path)
        m = report.symbol_gate_matrix
        assert m["005930"]["rate_limit"] == 2
        assert m["005930"]["exposure_per_symbol"] == 1
        assert m["000660"]["rate_limit"] == 1
        print(f"   ✅ 매트릭스: 005930 (rate=2, exposure=1), 000660 (rate=1)")
    finally:
        Path(audit_path).unlink()


def test_analyzer_time_filter():
    print("\n[19] Analyzer — 시간 필터")
    audit_path = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False).name
    try:
        records = [
            _make_record(decision="pass", decided_at_utc="2026-05-05T03:00:00+00:00"),
            _make_record(decision="reject", decided_at_utc="2026-05-06T03:00:00+00:00",
                         gate="rate_limit"),
            _make_record(decision="pass", decided_at_utc="2026-05-07T03:00:00+00:00"),
        ]
        _write_jsonl(Path(audit_path), records)

        analyzer = RejectionAnalyzer()
        # 5/6 만 분석
        report = analyzer.analyze(
            audit_path,
            since_utc=datetime(2026, 5, 6, tzinfo=timezone.utc),
            until_utc=datetime(2026, 5, 7, tzinfo=timezone.utc),
        )
        assert report.total_evaluations == 1
        assert report.reject_count == 1
        print(f"   ✅ 시간 필터로 1건만")
    finally:
        Path(audit_path).unlink()


def test_analyzer_corrupt_jsonl():
    print("\n[20] Analyzer — 깨진 JSONL skip")
    audit_path = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False).name
    try:
        with open(audit_path, "w") as f:
            f.write(json.dumps({
                "decision": "pass",
                "decided_at_utc": "2026-05-06T03:00:00+00:00",
            }) + "\n")
            f.write("not valid json\n")
            f.write("\n")
            f.write(json.dumps({
                "decision": "reject",
                "decided_at_utc": "2026-05-06T03:01:00+00:00",
                "rejected_by_gate": "rate_limit",
                "symbol": "005930",
            }) + "\n")

        analyzer = RejectionAnalyzer()
        report = analyzer.analyze(audit_path)
        assert report.total_evaluations == 2
        print(f"   ✅ 깨진 라인 skip, {report.total_evaluations}건")
    finally:
        Path(audit_path).unlink()


def test_analyzer_empty_file():
    print("\n[21] Analyzer — 빈 파일")
    audit_path = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False).name
    try:
        Path(audit_path).write_text("")
        analyzer = RejectionAnalyzer()
        report = analyzer.analyze(audit_path)
        assert report.total_evaluations == 0
        assert report.reject_count == 0
        print(f"   ✅ 빈 파일 → 0건 graceful")
    finally:
        Path(audit_path).unlink()


def test_analyzer_missing_file():
    print("\n[22] Analyzer — 없는 파일")
    analyzer = RejectionAnalyzer()
    report = analyzer.analyze("/nonexistent/path.jsonl")
    assert report.total_evaluations == 0
    print(f"   ✅ 없는 파일 → 0건 graceful")


def test_analyzer_window_validation():
    print("\n[23] Analyzer — window_minutes 검증")
    try:
        RejectionAnalyzer(window_minutes=0)
        assert False
    except ValueError:
        print(f"   ✅ window=0 거부")


def test_analyzer_tz_naive_rejected():
    print("\n[24] Analyzer — tz-naive since/until 거부")
    analyzer = RejectionAnalyzer()
    try:
        analyzer.analyze("/anywhere", since_utc=datetime(2026, 5, 6))
        assert False
    except ValueError as e:
        assert "tz-aware" in str(e)
        print(f"   ✅ tz-naive 거부")


# ─────────────────────────────────────────────────
# 출력 포맷 테스트
# ─────────────────────────────────────────────────

def test_report_to_json():
    print("\n[25] RejectionReport.to_json")
    audit_path = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False).name
    try:
        records = [
            _make_record(decision="pass", decided_at_utc="2026-05-06T03:00:00+00:00"),
            _make_record(decision="reject", decided_at_utc="2026-05-06T03:01:00+00:00",
                         gate="rate_limit", reason="too fast"),
        ]
        _write_jsonl(Path(audit_path), records)

        analyzer = RejectionAnalyzer()
        report = analyzer.analyze(audit_path)
        s = report.to_json()
        parsed = json.loads(s)
        assert parsed["summary"]["total_evaluations"] == 2
        assert parsed["summary"]["reject_count"] == 1
        assert "rate_limit" in parsed["by_gate"]
        print(f"   ✅ JSON 직렬화 OK")
    finally:
        Path(audit_path).unlink()


def test_report_to_markdown():
    print("\n[26] RejectionReport.to_markdown")
    audit_path = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False).name
    try:
        records = []
        for i in range(20):
            records.append(_make_record(
                decision="reject" if i % 3 == 0 else "pass",
                decided_at_utc=f"2026-05-06T03:{i:02d}:00+00:00",
                gate="rate_limit" if i % 3 == 0 else None,
            ))
        _write_jsonl(Path(audit_path), records)
        analyzer = RejectionAnalyzer()
        report = analyzer.analyze(audit_path)
        md = report.to_markdown()
        assert "리스크 거부 분석 리포트" in md
        assert "1. 요약" in md
        assert "2. 자동 진단" in md
        assert "3. 게이트별 분석" in md
        assert "rate_limit" in md
        print(f"   ✅ Markdown 생성 ({len(md)}자)")
    finally:
        Path(audit_path).unlink()


def test_report_to_html():
    print("\n[27] RejectionReport.to_html — XSS escape")
    # 사용자 입력에 <script> 포함 시 escape 검증
    report = RejectionReport(
        metadata={"source_path": "<script>alert('xss')</script>"},
        total_evaluations=10,
        pass_count=8,
        reject_count=2,
        rejection_rate=0.2,
    )
    html_out = report.to_html()
    assert "<!DOCTYPE html>" in html_out
    assert "<script>alert" not in html_out
    assert "&lt;script&gt;" in html_out or "&amp;lt;" in html_out
    print(f"   ✅ HTML 생성 + XSS escape")


def test_report_to_csv():
    print("\n[28] RejectionReport.to_csv_summary")
    audit_path = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False).name
    try:
        records = []
        for i in range(20):
            records.append(_make_record(
                decision="reject" if i < 5 else "pass",
                decided_at_utc=f"2026-05-06T03:{i:02d}:00+00:00",
                gate="rate_limit" if i < 5 else None,
            ))
        _write_jsonl(Path(audit_path), records)

        analyzer = RejectionAnalyzer()
        report = analyzer.analyze(audit_path)
        csv_str = report.to_csv_summary()
        # CSV 파싱 확인
        rows = list(csv.reader(io.StringIO(csv_str)))
        assert rows[0] == [
            "gate_name", "reject_count", "rate_in_total",
            "top_symbol", "top_symbol_count",
            "first_seen_utc", "last_seen_utc",
        ]
        # rate_limit이 5건
        gate_row = [r for r in rows[1:] if r[0] == "rate_limit"][0]
        assert int(gate_row[1]) == 5
        print(f"   ✅ CSV 생성 + 파싱 OK")
    finally:
        Path(audit_path).unlink()


def test_save_all_formats():
    print("\n[29] analyze_and_save — 4 포맷 저장")
    audit_path = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False).name
    try:
        records = [
            _make_record(decision="pass", decided_at_utc="2026-05-06T03:00:00+00:00"),
            _make_record(decision="reject", decided_at_utc="2026-05-06T03:01:00+00:00",
                         gate="rate_limit"),
        ]
        _write_jsonl(Path(audit_path), records)

        analyzer = RejectionAnalyzer()
        with tempfile.TemporaryDirectory() as tmp:
            paths = analyzer.analyze_and_save(
                audit_path, tmp,
                since_utc=datetime(2026, 5, 6, tzinfo=timezone.utc),
                formats=("json", "md", "html", "csv"),
            )
            assert "json" in paths and paths["json"].exists()
            assert "md" in paths and paths["md"].exists()
            assert "html" in paths and paths["html"].exists()
            assert "csv" in paths and paths["csv"].exists()

            # JSON 파싱
            d = json.loads(paths["json"].read_text())
            assert d["summary"]["total_evaluations"] == 2

            # CSV 행 수
            csv_rows = list(csv.reader(paths["csv"].read_text().splitlines()))
            assert len(csv_rows) >= 2  # header + 1 row

            print(f"   ✅ 4개 포맷 저장: {[p.name for p in paths.values()]}")
    finally:
        Path(audit_path).unlink()


# ─────────────────────────────────────────────────
# 보안 테스트
# ─────────────────────────────────────────────────

def test_secret_keywords_filtered():
    print("\n[30] Audit log secret 누출 방지")
    audit_path = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False).name
    try:
        with open(audit_path, "w") as f:
            f.write(json.dumps({
                "decision": "reject",
                "decided_at_utc": "2026-05-06T03:00:00+00:00",
                "rejected_by_gate": "rate_limit",
                "symbol": "005930",
                "app_secret": "BAD_SECRET_VAL",
                "auth_token": "BAD_TOKEN_VAL",
                "account_no": "12345-678",
            }) + "\n")

        analyzer = RejectionAnalyzer()
        report = analyzer.analyze(audit_path)
        s = report.to_json() + report.to_markdown() + report.to_html() + report.to_csv_summary()
        assert "BAD_SECRET_VAL" not in s
        assert "BAD_TOKEN_VAL" not in s
        assert "12345-678" not in s
        print(f"   ✅ 4개 출력 모두 비밀 누출 없음")
    finally:
        Path(audit_path).unlink()


# ─────────────────────────────────────────────────
# 통합 시나리오
# ─────────────────────────────────────────────────

def test_full_integration_with_diagnostics():
    print("\n[31] 전체 통합 — 진단 자동 생성")
    audit_path = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False).name
    try:
        records = []
        # 50건 평가 — 30건 reject (60% 거부율)
        # rate_limit 15건 (50% of rejects), exposure 10건 (33%), 기타 5건
        for i in range(15):
            records.append(_make_record(
                decision="reject",
                decided_at_utc=f"2026-05-06T03:{i:02d}:00+00:00",
                symbol="005930", gate="order_rate_limit",
            ))
        for i in range(10):
            records.append(_make_record(
                decision="reject",
                decided_at_utc=f"2026-05-06T03:{15 + i:02d}:00+00:00",
                symbol="005930", gate="exposure_per_symbol",
            ))
        for i in range(5):
            records.append(_make_record(
                decision="reject",
                decided_at_utc=f"2026-05-06T03:{25 + i:02d}:00+00:00",
                symbol="000660", gate="duplicate_order",
            ))
        # 20건 pass
        for i in range(20):
            records.append(_make_record(
                decision="pass",
                decided_at_utc=f"2026-05-06T04:{i:02d}:00+00:00",
                symbol="000660",
            ))
        _write_jsonl(Path(audit_path), records)

        analyzer = RejectionAnalyzer()
        report = analyzer.analyze(audit_path)
        assert report.total_evaluations == 50
        assert report.reject_count == 30
        assert abs(report.rejection_rate - 0.6) < 0.01

        # 진단 자동 생성
        codes = {f.code for f in report.diagnostic_findings}
        assert "high_rate_limit_rejections" in codes  # 15/30 = 50% > 30%
        assert "high_exposure_rejections" in codes    # 10/30 = 33% > 30%
        assert "single_symbol_dominance" in codes      # 005930: 25/30 = 83%

        # JSON / MD / HTML / CSV 모두 생성 가능
        report.to_json()
        report.to_markdown()
        report.to_html()
        report.to_csv_summary()
        print(f"   ✅ 진단 자동 생성: {sorted(codes)[:3]}...")
    finally:
        Path(audit_path).unlink()


if __name__ == "__main__":
    test_diagnose_insufficient_data()
    test_diagnose_kill_switch_critical()
    test_diagnose_daily_loss_critical()
    test_diagnose_high_rate_limit()
    test_diagnose_high_exposure()
    test_diagnose_sector_concentration()
    test_diagnose_market_state()
    test_diagnose_price_sanity()
    test_diagnose_single_symbol_dominance()
    test_diagnose_window_spike()
    test_diagnose_no_findings()
    test_diagnose_thresholds_override()
    test_analyzer_basic()
    test_analyzer_by_gate_top_symbols()
    test_analyzer_by_hour_kst()
    test_analyzer_rolling_window()
    test_analyzer_15min_window()
    test_analyzer_symbol_gate_matrix()
    test_analyzer_time_filter()
    test_analyzer_corrupt_jsonl()
    test_analyzer_empty_file()
    test_analyzer_missing_file()
    test_analyzer_window_validation()
    test_analyzer_tz_naive_rejected()
    test_report_to_json()
    test_report_to_markdown()
    test_report_to_html()
    test_report_to_csv()
    test_save_all_formats()
    test_secret_keywords_filtered()
    test_full_integration_with_diagnostics()
    print("\n🎉 모든 스모크 테스트 통과 (All smoke tests passed)")
