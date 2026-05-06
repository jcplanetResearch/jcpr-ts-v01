"""스모크 테스트 (Smoke Test) — Task 27 v0.1 Slippage Analyzer."""

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.execution.fill_store import FillStore
from src.execution.fills import Fill, FillSide
from src.pnl.slippage import SlippageAnalyzer, SlippageRecord


def _make_fill(*, fill_id, broker_order_no, side, qty, price,
               fee_krw="0", tax_krw="0", symbol="005930",
               filled_at=None, is_partial=False):
    return Fill(
        fill_id=fill_id,
        broker_order_no=broker_order_no,
        client_order_id=f"exec-{fill_id}",
        symbol=symbol, side=side, quantity=qty,
        price=Decimal(price),
        fee_krw=Decimal(fee_krw),
        tax_krw=Decimal(tax_krw),
        filled_at_utc=filled_at or datetime(2026, 5, 6, 9, 0, tzinfo=timezone.utc),
        received_at_utc=datetime.now(timezone.utc),
        source="test", is_partial=is_partial,
    )


def _make_store(fills):
    db = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False).name
    store = FillStore(db)
    if fills:
        store.upsert_many(fills)
    return store, db


# ─────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────

def test_buy_unfavorable_slippage():
    print("\n[1] BUY 불리한 슬리피지 (체결가 > 의도가)")
    fills = [_make_fill(
        fill_id="F1", broker_order_no="ORD-1",
        side=FillSide.BUY, qty=10, price="70200", fee_krw="100",
    )]
    store, db = _make_store(fills)
    try:
        analyzer = SlippageAnalyzer(store)
        rec = analyzer.analyze_execution(
            execution_id="exec-1", broker_order_no="ORD-1",
            intent_price_krw=Decimal("70000"),
            intent_quantity=10,
            side="buy", symbol="005930",
            intent_at_utc=datetime(2026, 5, 6, 9, tzinfo=timezone.utc),
        )
        # slippage = 70200 - 70000 = +200
        # bps = 200/70000 * 10000 ≈ 28.57
        assert rec.abs_slippage_krw == Decimal("200")
        assert rec.is_unfavorable is True
        assert abs(rec.slippage_bps - Decimal("28.5714")) < Decimal("0.01")
        assert rec.filled_quantity == 10
        assert not rec.is_partial
        print(f"   ✅ slippage=+200 KRW, bps={rec.slippage_bps:.2f}, unfavorable=True")
    finally:
        Path(db).unlink()


def test_sell_favorable_slippage():
    print("\n[2] SELL 유리한 슬리피지 (체결가 > 의도가)")
    # SELL은 fill > intent → 더 비싸게 팜 = 유리
    fills = [_make_fill(
        fill_id="F1", broker_order_no="ORD-2",
        side=FillSide.SELL, qty=10, price="71000",
        fee_krw="100", tax_krw="1500",
    )]
    store, db = _make_store(fills)
    try:
        analyzer = SlippageAnalyzer(store)
        rec = analyzer.analyze_execution(
            execution_id="exec-2", broker_order_no="ORD-2",
            intent_price_krw=Decimal("70000"),
            intent_quantity=10,
            side="sell", symbol="005930",
            intent_at_utc=datetime(2026, 5, 6, 9, tzinfo=timezone.utc),
        )
        # slippage_sell = intent - fill = 70000 - 71000 = -1000 (유리)
        assert rec.abs_slippage_krw == Decimal("-1000")
        assert rec.is_unfavorable is False  # 음수 = 유리
        assert rec.slippage_bps < 0
        # cost = (100+1500) / (10*70000) * 10000 = 22.857 bps
        assert abs(rec.cost_impact_bps - Decimal("22.8571")) < Decimal("0.01")
        print(f"   ✅ slippage=-1000 (유리), cost={rec.cost_impact_bps:.2f}bps")
    finally:
        Path(db).unlink()


def test_partial_fill_vwap():
    print("\n[3] 부분 체결 — VWAP 평균")
    # 같은 주문에 3건 체결: 5@70100, 3@70200, 2@70150 → VWAP
    fills = [
        _make_fill(fill_id="F1", broker_order_no="ORD-3", side=FillSide.BUY,
                   qty=5, price="70100", fee_krw="50",
                   filled_at=datetime(2026, 5, 6, 9, 0, tzinfo=timezone.utc),
                   is_partial=True),
        _make_fill(fill_id="F2", broker_order_no="ORD-3", side=FillSide.BUY,
                   qty=3, price="70200", fee_krw="30",
                   filled_at=datetime(2026, 5, 6, 9, 1, tzinfo=timezone.utc),
                   is_partial=True),
        _make_fill(fill_id="F3", broker_order_no="ORD-3", side=FillSide.BUY,
                   qty=2, price="70150", fee_krw="20",
                   filled_at=datetime(2026, 5, 6, 9, 2, tzinfo=timezone.utc),
                   is_partial=False),
    ]
    store, db = _make_store(fills)
    try:
        analyzer = SlippageAnalyzer(store)
        rec = analyzer.analyze_execution(
            execution_id="exec-3", broker_order_no="ORD-3",
            intent_price_krw=Decimal("70000"),
            intent_quantity=10,
            side="buy", symbol="005930",
            intent_at_utc=datetime(2026, 5, 6, 9, tzinfo=timezone.utc),
        )
        # VWAP = (5*70100 + 3*70200 + 2*70150) / 10
        #     = (350500 + 210600 + 140300) / 10 = 701400/10 = 70140
        assert rec.avg_fill_price_krw == Decimal("70140")
        assert rec.fill_count == 3
        assert rec.filled_quantity == 10
        # last_fill_at은 가장 늦은 체결
        assert rec.last_fill_at_utc == datetime(2026, 5, 6, 9, 2, tzinfo=timezone.utc)
        # 의도 수량과 체결 수량 일치 → is_partial=False
        # but 개별 fill에 is_partial이 있어 → True (방어적)
        # 의도수량 == 체결수량이지만 fills 중 partial 있으면 True
        assert rec.is_partial is True
        print(f"   ✅ VWAP=70140, fill_count=3")
    finally:
        Path(db).unlink()


def test_partial_quantity_underfill():
    print("\n[4] 의도 수량 미달 (intent=10, filled=5) → is_partial=True")
    fills = [_make_fill(
        fill_id="F1", broker_order_no="ORD-4",
        side=FillSide.BUY, qty=5, price="70000",
        is_partial=True,
    )]
    store, db = _make_store(fills)
    try:
        analyzer = SlippageAnalyzer(store)
        rec = analyzer.analyze_execution(
            execution_id="exec-4", broker_order_no="ORD-4",
            intent_price_krw=Decimal("70000"),
            intent_quantity=10,  # 의도 10, 체결 5
            side="buy", symbol="005930",
            intent_at_utc=datetime(2026, 5, 6, 9, tzinfo=timezone.utc),
        )
        assert rec.is_partial is True
        assert rec.filled_quantity == 5
        # slippage = 0 (체결가 == 의도가)
        assert rec.abs_slippage_krw == Decimal("0")
        print(f"   ✅ filled=5/10, is_partial=True, slippage=0")
    finally:
        Path(db).unlink()


def test_no_fills_returns_none():
    print("\n[5] 체결 없음 — None 반환")
    store, db = _make_store([])
    try:
        analyzer = SlippageAnalyzer(store)
        rec = analyzer.analyze_execution(
            execution_id="exec-5", broker_order_no="ORD-NONEXISTENT",
            intent_price_krw=Decimal("70000"),
            intent_quantity=10,
            side="buy", symbol="005930",
            intent_at_utc=datetime(2026, 5, 6, 9, tzinfo=timezone.utc),
        )
        assert rec is None
        print(f"   ✅ 체결 없으면 None")
    finally:
        Path(db).unlink()


def test_invalid_inputs():
    print("\n[6] 잘못된 입력 거부")
    store, db = _make_store([])
    try:
        analyzer = SlippageAnalyzer(store)

        # tz-naive
        try:
            analyzer.analyze_execution(
                execution_id="x", broker_order_no="x",
                intent_price_krw=Decimal("70000"),
                intent_quantity=10,
                side="buy", symbol="005930",
                intent_at_utc=datetime.now(),
            )
            assert False
        except ValueError as e:
            assert "tz-aware" in str(e)
            print(f"   ✅ tz-naive 거부")

        # 음수 가격
        try:
            analyzer.analyze_execution(
                execution_id="x", broker_order_no="x",
                intent_price_krw=Decimal("-100"),
                intent_quantity=10,
                side="buy", symbol="005930",
                intent_at_utc=datetime.now(timezone.utc),
            )
            assert False
        except ValueError:
            print(f"   ✅ 음수 가격 거부")

        # 잘못된 side
        try:
            analyzer.analyze_execution(
                execution_id="x", broker_order_no="x",
                intent_price_krw=Decimal("70000"),
                intent_quantity=10,
                side="hold", symbol="005930",
                intent_at_utc=datetime.now(timezone.utc),
            )
            assert False
        except ValueError as e:
            assert "side" in str(e).lower()
            print(f"   ✅ 잘못된 side 거부")
    finally:
        Path(db).unlink()


def test_audit_log_parsing():
    print("\n[7] Task 21 Audit log JSONL 파싱")
    audit_path = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False).name
    try:
        # 가짜 audit 기록 작성 (Task 21 형식)
        records = [
            {  # SUBMITTED
                "execution_id": "exec-A",
                "signal_id": "sig-001",
                "symbol": "005930", "side": "buy",
                "outcome": "submitted", "is_dry_run": False,
                "broker_order_no": "ORD-A",
                "aligned_price": "70000", "quantity": 10,
                "started_at_utc": "2026-05-06T09:00:00+00:00",
                "completed_at_utc": "2026-05-06T09:00:01+00:00",
            },
            {  # REJECTED — 분석 안 됨
                "execution_id": "exec-B",
                "outcome": "rejected", "broker_order_no": None,
                "started_at_utc": "2026-05-06T09:01:00+00:00",
            },
            {  # DRY-RUN — broker_order_no None — 스킵
                "execution_id": "exec-C",
                "symbol": "000660", "side": "buy",
                "outcome": "submitted", "is_dry_run": True,
                "broker_order_no": None,
                "aligned_price": "80000", "quantity": 5,
                "started_at_utc": "2026-05-06T09:02:00+00:00",
            },
            {  # SUBMITTED with fill
                "execution_id": "exec-D",
                "signal_id": "sig-002",
                "symbol": "000660", "side": "sell",
                "outcome": "submitted", "is_dry_run": False,
                "broker_order_no": "ORD-D",
                "aligned_price": "80000", "quantity": 5,
                "started_at_utc": "2026-05-06T10:00:00+00:00",
            },
        ]
        with open(audit_path, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

        # 매칭되는 fills (ORD-A, ORD-D)
        fills = [
            _make_fill(fill_id="FA", broker_order_no="ORD-A",
                       side=FillSide.BUY, qty=10, price="70150", fee_krw="100"),
            _make_fill(fill_id="FD", broker_order_no="ORD-D",
                       side=FillSide.SELL, qty=5, price="79800",
                       fee_krw="80", tax_krw="800",
                       symbol="000660"),
        ]
        store, db = _make_store(fills)
        try:
            analyzer = SlippageAnalyzer(store)
            recs = analyzer.analyze_executions_from_audit(audit_path)
            # ORD-A (submitted+broker), ORD-D (submitted+broker) — 2건
            # exec-B (rejected), exec-C (dry-run) 제외
            assert len(recs) == 2
            execs = sorted(r.execution_id for r in recs)
            assert execs == ["exec-A", "exec-D"]
            print(f"   ✅ {len(recs)}건 분석 (rejected/dry-run 제외)")
        finally:
            Path(db).unlink()
    finally:
        Path(audit_path).unlink()


def test_aggregate_statistics():
    print("\n[8] aggregate — 집계 통계")
    # 5건의 SlippageRecord 생성 (다양한 슬리피지)
    fills = []
    audit_records = []
    for i, (slip, fav) in enumerate([
        (50, True), (100, True), (30, True), (-20, False), (200, True),
    ]):
        intent = 70000
        fill_price = intent + slip  # buy: + → 불리
        fills.append(_make_fill(
            fill_id=f"F{i}", broker_order_no=f"ORD-{i}",
            side=FillSide.BUY, qty=10, price=str(fill_price),
            fee_krw="100",
        ))
        audit_records.append({
            "execution_id": f"exec-{i}",
            "symbol": "005930", "side": "buy",
            "outcome": "submitted", "is_dry_run": False,
            "broker_order_no": f"ORD-{i}",
            "aligned_price": str(intent), "quantity": 10,
            "started_at_utc": "2026-05-06T09:00:00+00:00",
        })

    audit_path = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False).name
    try:
        with open(audit_path, "w", encoding="utf-8") as f:
            for r in audit_records:
                f.write(json.dumps(r) + "\n")
        store, db = _make_store(fills)
        try:
            analyzer = SlippageAnalyzer(store)
            recs = analyzer.analyze_executions_from_audit(audit_path)
            assert len(recs) == 5

            agg = analyzer.aggregate(recs)
            assert agg["count"] == 5
            assert agg["unfavorable_count"] == 4  # 4 양수 + 1 음수
            assert Decimal(agg["unfavorable_pct"]) == Decimal("0.8")
            # 평균 slippage_bps: avg of [50, 100, 30, -20, 200]/70000 * 10000
            # = (360/5) / 70000 * 10000 = 72/70000 * 10000 = 10.2857
            avg_slip = Decimal(agg["avg_slippage_bps"])
            assert abs(avg_slip - Decimal("10.2857")) < Decimal("0.05")
            # by_symbol
            assert "005930" in agg["by_symbol"]
            assert agg["by_symbol"]["005930"]["count"] == 5
            print(f"   ✅ count=5, unfavorable=4 (80%), avg_slippage={avg_slip}bps")
            print(f"   ✅ by_symbol: {list(agg['by_symbol'].keys())}")
        finally:
            Path(db).unlink()
    finally:
        Path(audit_path).unlink()


def test_aggregate_empty():
    print("\n[9] aggregate — 빈 리스트")
    agg = SlippageAnalyzer.aggregate([])
    assert agg["count"] == 0
    assert agg["avg_slippage_bps"] is None
    print(f"   ✅ 빈 리스트 → count=0, avg=None")


def test_record_to_dict_serializable():
    print("\n[10] SlippageRecord.to_dict — JSON 직렬화 가능")
    fills = [_make_fill(
        fill_id="F1", broker_order_no="ORD-1",
        side=FillSide.BUY, qty=10, price="70200",
    )]
    store, db = _make_store(fills)
    try:
        analyzer = SlippageAnalyzer(store)
        rec = analyzer.analyze_execution(
            execution_id="exec-1", broker_order_no="ORD-1",
            intent_price_krw=Decimal("70000"), intent_quantity=10,
            side="buy", symbol="005930",
            intent_at_utc=datetime(2026, 5, 6, 9, tzinfo=timezone.utc),
        )
        d = rec.to_dict()
        # Decimal은 str로 변환됨
        assert isinstance(d["intent_price_krw"], str)
        assert isinstance(d["slippage_bps"], str)
        # JSON 직렬화 OK
        json_str = json.dumps(d)
        assert "70000" in json_str
        print(f"   ✅ to_dict + JSON 직렬화 OK")
    finally:
        Path(db).unlink()


if __name__ == "__main__":
    test_buy_unfavorable_slippage()
    test_sell_favorable_slippage()
    test_partial_fill_vwap()
    test_partial_quantity_underfill()
    test_no_fills_returns_none()
    test_invalid_inputs()
    test_audit_log_parsing()
    test_aggregate_statistics()
    test_aggregate_empty()
    test_record_to_dict_serializable()
    print("\n🎉 모든 스모크 테스트 통과 (All smoke tests passed)")
