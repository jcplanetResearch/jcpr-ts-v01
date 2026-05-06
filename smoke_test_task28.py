"""스모크 테스트 (Smoke Test) — Task 28 v0.1 Reconciliation."""

import json
import sys
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent))

from src.brokers.kis.account import AccountSnapshot, PositionInfo
from src.execution.fills import Fill, FillSide
from src.pnl.position_ledger import PositionLedger
from src.pnl.position_state import PositionState
from src.pnl.position_store import PositionStore
from src.pnl.reconciliation import (
    MismatchType, PositionMismatch, Reconciler, ReconciliationReport,
)


def _mock_account_snapshot(positions_dict, cash_krw="10000000"):
    """KISAccount mock — fetch_account_snapshot()이 주어진 포지션을 반환."""
    pos_objects = {}
    for sym, p in positions_dict.items():
        pos_objects[sym] = PositionInfo(
            symbol=sym,
            quantity=p["quantity"],
            available_quantity=p.get("available", p["quantity"]),
            avg_price_krw=Decimal(str(p["avg_price"])),
            current_price_krw=Decimal(str(p.get("current_price", p["avg_price"]))),
            market_value_krw=Decimal(str(p["quantity"])) * Decimal(str(p.get("current_price", p["avg_price"]))),
            unrealized_pnl_krw=Decimal("0"),
            unrealized_pnl_pct=Decimal("0"),
        )
    snap = AccountSnapshot(
        captured_at_utc=datetime.now(timezone.utc),
        cash_krw=Decimal(cash_krw),
        available_cash_krw=Decimal(cash_krw),
        total_evaluation_krw=Decimal(cash_krw) + sum(
            (p.market_value_krw for p in pos_objects.values()), Decimal("0")
        ),
        total_purchase_krw=Decimal("0"),
        total_unrealized_pnl_krw=Decimal("0"),
        positions=pos_objects,
    )
    mock = MagicMock()
    mock.fetch_account_snapshot.return_value = snap
    return mock


def _make_ledger_with_positions(states_dict):
    """메모리 ledger에 포지션 직접 주입."""
    db = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False).name
    ledger = PositionLedger(PositionStore(db))
    for sym, p in states_dict.items():
        ledger._store.upsert(PositionState(
            symbol=sym,
            quantity=p["quantity"],
            avg_cost_krw=Decimal(str(p["avg_cost"])),
            realized_pnl_krw=Decimal("0"),
            total_fees_krw=Decimal("0"),
            total_taxes_krw=Decimal("0"),
            last_updated_utc=datetime.now(timezone.utc),
            fills_processed=1,
        ))
    return ledger, db


# ─────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────

def test_all_match():
    print("\n[1] 완전 일치 — severity=ok")
    broker = _mock_account_snapshot({
        "005930": {"quantity": 10, "avg_price": "70000"},
        "000660": {"quantity": 5, "avg_price": "120000"},
    })
    ledger, db = _make_ledger_with_positions({
        "005930": {"quantity": 10, "avg_cost": "70000"},
        "000660": {"quantity": 5, "avg_cost": "120000"},
    })
    try:
        rec = Reconciler(broker, ledger)
        report = rec.reconcile()
        assert report.all_matched()
        assert report.severity() == "ok"
        assert len(report.matches) == 2
        assert len(report.mismatches) == 0
        print(f"   ✅ severity=ok, matches=2")
    finally:
        Path(db).unlink()


def test_broker_only():
    print("\n[2] BROKER_ONLY 종목")
    broker = _mock_account_snapshot({
        "005930": {"quantity": 10, "avg_price": "70000"},
        "000660": {"quantity": 5, "avg_price": "120000"},  # ledger엔 없음
    })
    ledger, db = _make_ledger_with_positions({
        "005930": {"quantity": 10, "avg_cost": "70000"},
    })
    try:
        rec = Reconciler(broker, ledger)
        report = rec.reconcile()
        assert report.severity() == "major"
        assert len(report.mismatches) == 1
        m = report.mismatches[0]
        assert m.type == MismatchType.BROKER_ONLY
        assert m.symbol == "000660"
        assert m.broker_quantity == 5
        assert m.ledger_quantity is None
        print(f"   ✅ {m.symbol} BROKER_ONLY, severity=major")
    finally:
        Path(db).unlink()


def test_ledger_only():
    print("\n[3] LEDGER_ONLY 종목")
    broker = _mock_account_snapshot({
        "005930": {"quantity": 10, "avg_price": "70000"},
    })
    ledger, db = _make_ledger_with_positions({
        "005930": {"quantity": 10, "avg_cost": "70000"},
        "035720": {"quantity": 3, "avg_cost": "55000"},  # broker엔 없음
    })
    try:
        rec = Reconciler(broker, ledger)
        report = rec.reconcile()
        assert report.severity() == "major"
        assert len(report.mismatches) == 1
        m = report.mismatches[0]
        assert m.type == MismatchType.LEDGER_ONLY
        assert m.symbol == "035720"
        assert m.broker_quantity is None
        assert m.ledger_quantity == 3
        assert m.diff_quantity == -3  # broker 0 - ledger 3
        print(f"   ✅ {m.symbol} LEDGER_ONLY, diff_qty={m.diff_quantity}")
    finally:
        Path(db).unlink()


def test_quantity_mismatch():
    print("\n[4] 수량 불일치 (QUANTITY)")
    broker = _mock_account_snapshot({
        "005930": {"quantity": 15, "avg_price": "70000"},
    })
    ledger, db = _make_ledger_with_positions({
        "005930": {"quantity": 10, "avg_cost": "70000"},
    })
    try:
        rec = Reconciler(broker, ledger)
        report = rec.reconcile()
        assert report.severity() == "major"
        m = report.mismatches[0]
        assert m.type == MismatchType.QUANTITY
        assert m.broker_quantity == 15
        assert m.ledger_quantity == 10
        assert m.diff_quantity == 5
        print(f"   ✅ QUANTITY mismatch: diff={m.diff_quantity}")
    finally:
        Path(db).unlink()


def test_avg_price_within_tolerance_absolute():
    print("\n[5] 평균가 허용 오차 내 (절대) — match")
    broker = _mock_account_snapshot({
        "005930": {"quantity": 10, "avg_price": "70000.5"},  # 0.5원 차이
    })
    ledger, db = _make_ledger_with_positions({
        "005930": {"quantity": 10, "avg_cost": "70000"},
    })
    try:
        # 절대 1원 허용
        rec = Reconciler(broker, ledger, avg_price_tolerance_krw=Decimal("1"))
        report = rec.reconcile()
        assert report.all_matched()
        print(f"   ✅ 0.5원 차이 → 허용 (절대 ±1원)")
    finally:
        Path(db).unlink()


def test_avg_price_within_tolerance_bps():
    print("\n[6] 평균가 허용 오차 내 (bps 상대) — match")
    # 70000 * 0.01% = 7원 차이까지 허용 (1bp)
    broker = _mock_account_snapshot({
        "005930": {"quantity": 10, "avg_price": "70005"},  # 5원 차이
    })
    ledger, db = _make_ledger_with_positions({
        "005930": {"quantity": 10, "avg_cost": "70000"},
    })
    try:
        # 절대 1원 — 5원 초과
        # bps 1 (=0.01%): 70000 * 0.0001 = 7원까지 허용
        rec = Reconciler(
            broker, ledger,
            avg_price_tolerance_krw=Decimal("1"),
            avg_price_tolerance_bps=Decimal("1"),
        )
        report = rec.reconcile()
        # 5원은 절대(1원) 초과 but bps(7원) 이내 → 일치
        assert report.all_matched()
        print(f"   ✅ 5원 차이 → bps 허용 (1bp = 7원 이내)")
    finally:
        Path(db).unlink()


def test_avg_price_exceeds_tolerance():
    print("\n[7] 평균가 허용 오차 초과 — AVG_PRICE mismatch")
    broker = _mock_account_snapshot({
        "005930": {"quantity": 10, "avg_price": "71000"},  # 1000원 차이
    })
    ledger, db = _make_ledger_with_positions({
        "005930": {"quantity": 10, "avg_cost": "70000"},
    })
    try:
        rec = Reconciler(broker, ledger)
        report = rec.reconcile()
        # 1000원 차이 — 절대 1원 초과 + bps 1bp = 7원 초과
        assert report.severity() == "minor"  # AVG_PRICE만 → minor
        m = report.mismatches[0]
        assert m.type == MismatchType.AVG_PRICE
        assert m.diff_avg_price_krw == Decimal("1000")
        # 수량은 일치이므로 diff_quantity=0
        assert m.diff_quantity == 0
        print(f"   ✅ AVG_PRICE diff=1000, severity=minor")
    finally:
        Path(db).unlink()


def test_quantity_takes_priority_over_avg_price():
    print("\n[8] 수량 + 평균가 모두 불일치 → QUANTITY만 보고")
    broker = _mock_account_snapshot({
        "005930": {"quantity": 15, "avg_price": "75000"},
    })
    ledger, db = _make_ledger_with_positions({
        "005930": {"quantity": 10, "avg_cost": "70000"},
    })
    try:
        rec = Reconciler(broker, ledger)
        report = rec.reconcile()
        # 한 종목당 mismatch 1개만 (수량 우선)
        assert len(report.mismatches) == 1
        assert report.mismatches[0].type == MismatchType.QUANTITY
        print(f"   ✅ 수량 우선 (1개 mismatch만)")
    finally:
        Path(db).unlink()


def test_multiple_mismatch_types():
    print("\n[9] 다양한 mismatch 동시 발생")
    broker = _mock_account_snapshot({
        "005930": {"quantity": 10, "avg_price": "70000"},   # match
        "000660": {"quantity": 5, "avg_price": "120000"},   # broker_only
        "035720": {"quantity": 7, "avg_price": "55000"},    # quantity mismatch
    })
    ledger, db = _make_ledger_with_positions({
        "005930": {"quantity": 10, "avg_cost": "70000"},    # match
        "035720": {"quantity": 5, "avg_cost": "55000"},     # qty diff (broker 7 vs ledger 5)
        "247540": {"quantity": 2, "avg_cost": "300000"},    # ledger_only
    })
    try:
        rec = Reconciler(broker, ledger)
        report = rec.reconcile()
        assert len(report.matches) == 1  # 005930
        assert len(report.mismatches) == 3  # 000660, 035720, 247540
        types = {m.type for m in report.mismatches}
        assert MismatchType.BROKER_ONLY in types
        assert MismatchType.LEDGER_ONLY in types
        assert MismatchType.QUANTITY in types
        assert report.severity() == "major"
        bt = report.by_type()
        assert bt["broker_only"] == 1
        assert bt["ledger_only"] == 1
        assert bt["quantity"] == 1
        print(f"   ✅ 3 mismatches: {bt}")
    finally:
        Path(db).unlink()


def test_severity_classification():
    print("\n[10] severity 분류 (ok/minor/major)")
    # ok
    broker_ok = _mock_account_snapshot({"005930": {"quantity": 10, "avg_price": "70000"}})
    ledger_ok, db1 = _make_ledger_with_positions({"005930": {"quantity": 10, "avg_cost": "70000"}})
    try:
        assert Reconciler(broker_ok, ledger_ok).reconcile().severity() == "ok"
        print(f"   ✅ ok")
    finally:
        Path(db1).unlink()

    # minor (avg_price만)
    broker_m = _mock_account_snapshot({"005930": {"quantity": 10, "avg_price": "71000"}})
    ledger_m, db2 = _make_ledger_with_positions({"005930": {"quantity": 10, "avg_cost": "70000"}})
    try:
        assert Reconciler(broker_m, ledger_m).reconcile().severity() == "minor"
        print(f"   ✅ minor (avg_price만)")
    finally:
        Path(db2).unlink()

    # major (수량 차이)
    broker_M = _mock_account_snapshot({"005930": {"quantity": 15, "avg_price": "70000"}})
    ledger_M, db3 = _make_ledger_with_positions({"005930": {"quantity": 10, "avg_cost": "70000"}})
    try:
        assert Reconciler(broker_M, ledger_M).reconcile().severity() == "major"
        print(f"   ✅ major (수량)")
    finally:
        Path(db3).unlink()


def test_jsonl_audit_log():
    print("\n[11] JSONL audit 기록")
    broker = _mock_account_snapshot({
        "005930": {"quantity": 10, "avg_price": "71000"},
    })
    ledger, db = _make_ledger_with_positions({
        "005930": {"quantity": 10, "avg_cost": "70000"},
    })
    audit_path = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False).name
    try:
        rec = Reconciler(broker, ledger)
        report = rec.reconcile()
        rec.report_to_jsonl(report, audit_path)

        # 다시 reconcile + append
        report2 = rec.reconcile()
        rec.report_to_jsonl(report2, audit_path)

        with open(audit_path, "r", encoding="utf-8") as f:
            lines = [l for l in f.readlines() if l.strip()]
        assert len(lines) == 2

        rec_dict = json.loads(lines[0])
        assert rec_dict["severity"] == "minor"
        assert rec_dict["mismatch_count"] == 1
        # 비밀 노출 검사
        raw = "\n".join(lines)
        assert "app_key" not in raw.lower()
        assert "app_secret" not in raw.lower()
        print(f"   ✅ {len(lines)}건 기록, 비밀 노출 없음")
    finally:
        Path(db).unlink()
        Path(audit_path).unlink()


def test_to_dict_serializable():
    print("\n[12] ReconciliationReport.to_dict — JSON 직렬화")
    broker = _mock_account_snapshot({
        "005930": {"quantity": 10, "avg_price": "71000"},
        "000660": {"quantity": 5, "avg_price": "120000"},
    })
    ledger, db = _make_ledger_with_positions({
        "005930": {"quantity": 10, "avg_cost": "70000"},
    })
    try:
        rec = Reconciler(broker, ledger)
        report = rec.reconcile()
        d = report.to_dict()
        # JSON 직렬화 가능
        json_str = json.dumps(d, ensure_ascii=False)
        assert "severity" in d
        assert "mismatches" in d
        assert "by_type" in d
        # Decimal은 str로 변환
        assert isinstance(d["broker_cash_krw"], str)
        print(f"   ✅ to_dict + JSON 직렬화 OK")
    finally:
        Path(db).unlink()


def test_invalid_tolerance():
    print("\n[13] 음수 허용 오차 거부")
    broker = _mock_account_snapshot({})
    ledger, db = _make_ledger_with_positions({})
    try:
        try:
            Reconciler(broker, ledger, avg_price_tolerance_krw=Decimal("-1"))
            assert False
        except ValueError as e:
            assert "음수" in str(e)
            print(f"   ✅ 음수 tolerance 거부")
    finally:
        Path(db).unlink()


def test_read_only_no_side_effects():
    print("\n[14] Reconcile은 read-only (ledger 미변경)")
    broker = _mock_account_snapshot({
        "005930": {"quantity": 15, "avg_price": "70000"},  # mismatch
    })
    ledger, db = _make_ledger_with_positions({
        "005930": {"quantity": 10, "avg_cost": "70000"},
    })
    try:
        before = ledger.get("005930")
        rec = Reconciler(broker, ledger)
        report = rec.reconcile()
        after = ledger.get("005930")
        # mismatch 발견했지만 ledger 변경 없음
        assert before.quantity == after.quantity
        assert before.avg_cost_krw == after.avg_cost_krw
        assert len(report.mismatches) == 1  # 발견
        print(f"   ✅ mismatch 발견했으나 ledger 미변경 (read-only)")
    finally:
        Path(db).unlink()


if __name__ == "__main__":
    test_all_match()
    test_broker_only()
    test_ledger_only()
    test_quantity_mismatch()
    test_avg_price_within_tolerance_absolute()
    test_avg_price_within_tolerance_bps()
    test_avg_price_exceeds_tolerance()
    test_quantity_takes_priority_over_avg_price()
    test_multiple_mismatch_types()
    test_severity_classification()
    test_jsonl_audit_log()
    test_to_dict_serializable()
    test_invalid_tolerance()
    test_read_only_no_side_effects()
    print("\n🎉 모든 스모크 테스트 통과 (All smoke tests passed)")
