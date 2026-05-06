"""스모크 테스트 (Smoke Test) — Task 13 v0.1 Quote Snapshot."""

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.data.quote_schema import DepthLevel, QuoteSnapshot
from src.data.dummy_quote_source import DummyQuoteSource
from src.data.quote_store import QuoteStore
from src.risk.gates.price_sanity_gate import PriceSanityGate
from src.risk.gates.base import RiskContext


def test_quote_snapshot_validation():
    print("\n[1] QuoteSnapshot 검증 (validation)")
    now = datetime.now(timezone.utc)

    # 정상
    snap = QuoteSnapshot(
        symbol="005930",
        captured_at_utc=now, received_at_utc=now,
        best_bid=Decimal("70000"), best_ask=Decimal("70100"),
        best_bid_size=100, best_ask_size=80,
    )
    assert snap.mid_quote() == Decimal("70050")
    assert snap.spread() == Decimal("100")
    print(f"   ✅ mid={snap.mid_quote()}, spread={snap.spread()}, bps={snap.spread_bps():.2f}")

    # crossed market 거부 (ask < bid)
    try:
        QuoteSnapshot(
            symbol="005930",
            captured_at_utc=now, received_at_utc=now,
            best_bid=Decimal("70100"), best_ask=Decimal("70000"),
            best_bid_size=100, best_ask_size=80,
        )
        assert False
    except ValueError as e:
        print(f"   ✅ crossed market 거부: {e}")

    # tz-naive 거부
    try:
        QuoteSnapshot(
            symbol="005930",
            captured_at_utc=datetime.now(),  # naive
            received_at_utc=now,
            best_bid=Decimal("70000"), best_ask=Decimal("70100"),
            best_bid_size=100, best_ask_size=80,
        )
        assert False
    except ValueError as e:
        print(f"   ✅ tz-naive 거부")


def test_imbalance_and_metrics():
    print("\n[2] 파생 지표 (imbalance, depth_imbalance)")
    now = datetime.now(timezone.utc)
    depth = tuple(
        DepthLevel(level=i, price=Decimal("70000") + Decimal(i*100),
                   bid_size=200 - i*10, ask_size=100 - i*5)
        for i in range(1, 6)
    )
    snap = QuoteSnapshot(
        symbol="005930",
        captured_at_utc=now, received_at_utc=now,
        best_bid=Decimal("70000"), best_ask=Decimal("70100"),
        best_bid_size=200, best_ask_size=100,  # 매수 우위
        depth_levels=depth,
    )
    imb = snap.imbalance()
    assert imb is not None and imb > 0  # 매수 우위
    print(f"   ✅ imbalance={imb:.4f} (매수 우위)")
    
    depth_imb = snap.depth_imbalance(levels=5)
    assert depth_imb is not None and depth_imb > 0
    print(f"   ✅ depth_imbalance(5)={depth_imb:.4f}")


def test_staleness():
    print("\n[3] 신선도 (staleness)")
    captured = datetime(2026, 5, 6, 0, 0, 0, tzinfo=timezone.utc)
    snap = QuoteSnapshot(
        symbol="005930",
        captured_at_utc=captured, received_at_utc=captured,
        best_bid=Decimal("70000"), best_ask=Decimal("70100"),
        best_bid_size=100, best_ask_size=80,
    )
    # 5초 후 — fresh
    now = captured + timedelta(seconds=5)
    assert not snap.is_stale(now, max_age_sec=30)
    # 60초 후 — stale
    now = captured + timedelta(seconds=60)
    assert snap.is_stale(now, max_age_sec=30)
    # 미래 시각의 호가 — stale (시계 오류)
    future_snap = QuoteSnapshot(
        symbol="005930",
        captured_at_utc=captured + timedelta(seconds=100),
        received_at_utc=captured + timedelta(seconds=100),
        best_bid=Decimal("70000"), best_ask=Decimal("70100"),
        best_bid_size=100, best_ask_size=80,
    )
    assert future_snap.is_stale(captured, max_age_sec=30)
    print(f"   ✅ stale 판정 정확")


def test_dummy_quote_source():
    print("\n[4] DummyQuoteSource — 합성 호가")
    src = DummyQuoteSource(base_price=Decimal("70000"), depth_levels=10)
    assert src.is_live is False  # 실거래 차단

    fixed = datetime(2026, 5, 6, 0, 0, 0, tzinfo=timezone.utc)
    snap = src.snapshot("005930", fixed_time=fixed)
    assert snap.symbol == "005930"
    assert snap.best_ask > snap.best_bid
    assert len(snap.depth_levels) == 10
    # 호가단위 정합 확인 (KRX 70000원대는 100원 tick)
    assert snap.best_ask - snap.best_bid == 100  # 1 tick
    # 결정론
    snap2 = src.snapshot("005930", fixed_time=fixed)
    assert snap.best_bid == snap2.best_bid
    assert snap.best_ask == snap2.best_ask
    print(f"   ✅ bid={snap.best_bid}, ask={snap.best_ask}, mid={snap.mid_quote()}, depth=10")


def test_quote_store_roundtrip():
    print("\n[5] QuoteStore — upsert + fetch 왕복")
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db_path = f.name

    try:
        store = QuoteStore(db_path)
        src = DummyQuoteSource(depth_levels=10)
        fixed = datetime(2026, 5, 6, 0, 0, 0, tzinfo=timezone.utc)
        snap = src.snapshot("005930", fixed_time=fixed)
        store.upsert(snap)

        latest = store.latest("005930")
        assert latest is not None
        assert latest.best_bid == snap.best_bid
        assert latest.best_ask == snap.best_ask
        assert len(latest.depth_levels) == 10
        # 멱등 (재upsert)
        store.upsert(snap)
        latest2 = store.latest("005930")
        assert latest2.best_bid == snap.best_bid
        # depth 또한 중복되지 않음
        depth_count = len(latest2.depth_levels)
        assert depth_count == 10
        print(f"   ✅ 왕복 일치, 멱등성 OK (depth={depth_count})")
    finally:
        Path(db_path).unlink()


def test_latest_fresh():
    print("\n[6] latest_fresh — stale 거부")
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db_path = f.name

    try:
        store = QuoteStore(db_path)
        src = DummyQuoteSource()
        captured = datetime(2026, 5, 6, 0, 0, 0, tzinfo=timezone.utc)
        snap = src.snapshot("005930", fixed_time=captured)
        store.upsert(snap)

        # 5초 후 조회 — fresh
        now_fresh = captured + timedelta(seconds=5)
        latest = store.latest_fresh("005930", now_fresh, max_age_sec=30)
        assert latest is not None
        print(f"   ✅ 5초 후 — fresh ({latest.age_seconds(now_fresh):.1f}s)")

        # 60초 후 조회 — stale → None
        now_stale = captured + timedelta(seconds=60)
        latest = store.latest_fresh("005930", now_stale, max_age_sec=30)
        assert latest is None
        print(f"   ✅ 60초 후 — stale 거부 (None)")
    finally:
        Path(db_path).unlink()


def test_price_sanity_gate_with_quote_store():
    print("\n[7] PriceSanityGate — QuoteStore 통합")
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db_path = f.name

    try:
        store = QuoteStore(db_path)
        src = DummyQuoteSource()
        captured = datetime(2026, 5, 6, 0, 0, 0, tzinfo=timezone.utc)
        snap = src.snapshot("005930", fixed_time=captured)
        store.upsert(snap)
        mid = snap.mid_quote()

        gate = PriceSanityGate(
            max_deviation_pct=Decimal("0.05"),
            quote_store=store,
            max_quote_age_sec=30,
        )

        # 케이스 1: mid 근처 가격 — 통과
        ctx_pass = RiskContext(
            symbol="005930", side="buy", quantity=10,
            price=mid,  # 정확히 mid
            estimated_cost_krw=mid * 10,
            strategy_id="test", intent_id="i1", instrument_type="stock",
            equity_krw=Decimal("10000000"), available_cash_krw=Decimal("5000000"),
            daily_realized_pnl_krw=Decimal("0"), open_positions={}, pending_orders=[],
            market_now_utc=captured + timedelta(seconds=5),
            market_is_open=True,
            last_quote_price=None,  # 폴백 없음 — quote_store만 사용
            last_order_at_utc=None, last_order_for_symbol_utc=None,
        )
        result = gate.evaluate(ctx_pass)
        assert result.outcome == "pass"
        print(f"   ✅ mid 가격 통과: ref_source={result.detail['ref_source']}")

        # 케이스 2: mid 대비 +10% — 거부
        ctx_reject = RiskContext(
            **{**ctx_pass.__dict__, "price": mid * Decimal("1.10"), "intent_id": "i2"}
        )
        result = gate.evaluate(ctx_reject)
        assert result.outcome == "reject"
        print(f"   ✅ +10% 가격 거부: {result.reason}")

        # 케이스 3: stale 호가 — fallback to last_quote_price
        ctx_stale = RiskContext(
            **{**ctx_pass.__dict__,
               "market_now_utc": captured + timedelta(seconds=120),  # stale
               "last_quote_price": mid,  # 폴백 제공
               "intent_id": "i3"}
        )
        result = gate.evaluate(ctx_stale)
        # 폴백으로 ctx_last_quote 사용 → 통과
        assert result.outcome == "pass"
        assert "ctx_last_quote" in result.detail["ref_source"]
        print(f"   ✅ stale 호가 → 폴백: ref_source={result.detail['ref_source']}")

        # 케이스 4: stale + 폴백 없음 — 거부
        ctx_no_ref = RiskContext(
            **{**ctx_pass.__dict__,
               "market_now_utc": captured + timedelta(seconds=120),
               "last_quote_price": None,
               "intent_id": "i4"}
        )
        result = gate.evaluate(ctx_no_ref)
        assert result.outcome == "reject"
        assert "stale" in result.reason.lower() or "기준가" in result.reason
        print(f"   ✅ stale + 폴백 없음 → 거부 (fail-closed)")
    finally:
        Path(db_path).unlink()


def test_backward_compat():
    print("\n[8] PriceSanityGate — quote_store 없이 (기존 동작)")
    gate = PriceSanityGate(max_deviation_pct=Decimal("0.05"))  # quote_store 미제공
    captured = datetime.now(timezone.utc)

    ctx = RiskContext(
        symbol="005930", side="buy", quantity=10,
        price=Decimal("70000"),
        estimated_cost_krw=Decimal("700000"),
        strategy_id="test", intent_id="i1", instrument_type="stock",
        equity_krw=Decimal("10000000"), available_cash_krw=Decimal("5000000"),
        daily_realized_pnl_krw=Decimal("0"), open_positions={}, pending_orders=[],
        market_now_utc=captured, market_is_open=True,
        last_quote_price=Decimal("70000"),  # 직접 제공
        last_order_at_utc=None, last_order_for_symbol_utc=None,
    )
    result = gate.evaluate(ctx)
    assert result.outcome == "pass"
    assert result.detail["ref_source"] == "ctx_last_quote"
    print(f"   ✅ 하위 호환 OK: ref_source={result.detail['ref_source']}")


def test_purge():
    print("\n[9] purge_older_than")
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db_path = f.name

    try:
        store = QuoteStore(db_path)
        src = DummyQuoteSource()
        old_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
        new_time = datetime(2026, 5, 6, tzinfo=timezone.utc)
        store.upsert(src.snapshot("005930", fixed_time=old_time))
        store.upsert(src.snapshot("005930", fixed_time=new_time))

        n_purged = store.purge_older_than(datetime(2026, 3, 1, tzinfo=timezone.utc))
        assert n_purged == 1
        latest = store.latest("005930")
        assert latest.captured_at_utc == new_time
        print(f"   ✅ {n_purged}개 삭제, 최신 보존")
    finally:
        Path(db_path).unlink()


if __name__ == "__main__":
    test_quote_snapshot_validation()
    test_imbalance_and_metrics()
    test_staleness()
    test_dummy_quote_source()
    test_quote_store_roundtrip()
    test_latest_fresh()
    test_price_sanity_gate_with_quote_store()
    test_backward_compat()
    test_purge()
    print("\n🎉 모든 스모크 테스트 통과 (All smoke tests passed)")
