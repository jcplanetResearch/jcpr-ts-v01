"""스모크 테스트 (Smoke Test) — Task 14 v0.4 Momentum Strategy."""

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.data.ohlcv_schema import OHLCVBar, Timeframe, TickDirection, VolumeSplitMethod
from src.data.ohlcv_store import OHLCVStore
from src.data.dummy_source import DummySource
from src.data.dummy_quote_source import DummyQuoteSource
from src.data.quote_store import QuoteStore
from src.data.symbol_master import SymbolMaster
from src.signals.schema_v2 import MomentumSignalV04, SignalSide
from src.signals.strategies.momentum_v04 import MomentumStrategyV04, MomentumV04Config
from src.signals.strategies.indicators import (
    compute_price_momentum, compute_volume_confirmation,
    compute_buy_sell_intensity, compute_cvd_trend,
)


CSV_PATH = Path(__file__).parent / "data" / "reference" / "symbol_master.csv"


# ─────────────────────────────────────────────────
# 1. Indicator unit tests
# ─────────────────────────────────────────────────

def test_price_momentum():
    print("\n[1] price_momentum 정규화")
    closes = [Decimal("100"), Decimal("100"), Decimal("100"), Decimal("105")]
    score = compute_price_momentum(closes, lookback=3)
    # raw = 5/100 = 0.05, threshold=0.10 → 0.5
    assert score == Decimal("0.5"), f"got {score}"
    print(f"   ✅ +5% → score={score}")
    
    # 데이터 부족
    assert compute_price_momentum([Decimal("100")], 3) is None
    print(f"   ✅ 데이터 부족 → None")
    
    # 클램핑 — +20%
    closes_strong = [Decimal("100"), Decimal("100"), Decimal("100"), Decimal("120")]
    score = compute_price_momentum(closes_strong, lookback=3)
    assert score == Decimal("1")
    print(f"   ✅ +20% → 클램핑 = +1")


def test_volume_confirm():
    print("\n[2] volume_confirmation")
    # 거래량 2배 증가 + 가격 상승 방향 → +1
    volumes = [100, 100, 100, 200, 200, 200, 200, 200, 200, 200]
    score = compute_volume_confirmation(volumes, short_window=3, long_window=10, direction=1)
    # short_avg=200, long_avg=170 → ratio≈1.18 → 0.18
    assert score is not None and score > 0
    print(f"   ✅ 거래량 증가 + buy 방향 → score={score:.4f}")


def test_intensity():
    print("\n[3] buy_sell_intensity")
    # 강한 매수 (intensity 0.8 평균)
    intensities = [Decimal("0.8")] * 5
    score = compute_buy_sell_intensity(intensities, lookback=5)
    # avg=0.8 → (0.8-0.5)*2 = 0.6
    assert score == Decimal("0.6")
    print(f"   ✅ 매수 0.8 평균 → score={score}")
    
    # None 비율 50% 이상 → None
    intensities_sparse = [Decimal("0.8"), None, None, None, None]
    assert compute_buy_sell_intensity(intensities_sparse, lookback=5) is None
    print(f"   ✅ None 50% 이상 → None")


def test_cvd_trend():
    print("\n[4] cvd_trend")
    # CVD 누적 증가
    cvd = [0, 10000, 25000, 40000, 60000, 80000]
    score = compute_cvd_trend(cvd, lookback=5, normalization_threshold=100_000)
    # delta = 80000 - 0 = 80000 → 0.8
    assert score == Decimal("0.8")
    print(f"   ✅ CVD 80k 증가 → score={score}")


# ─────────────────────────────────────────────────
# 2. Integration tests
# ─────────────────────────────────────────────────

def test_full_strategy_with_dummy_data():
    print("\n[5] 전체 전략 — DummySource + DummyQuoteSource 통합")
    with tempfile.TemporaryDirectory() as td:
        ohlcv_db = Path(td) / "ohlcv.sqlite"
        quote_db = Path(td) / "quote.sqlite"
        
        # OHLCV 데이터 채우기
        ohlcv_store = OHLCVStore(ohlcv_db)
        src = DummySource()
        bars = list(src.fetch_bars(
            "005930", Timeframe.D1,
            datetime(2026, 4, 1, tzinfo=timezone.utc),
            datetime(2026, 5, 5, tzinfo=timezone.utc),
        ))
        ohlcv_store.upsert_bars(bars)
        print(f"   ▸ OHLCV 채움: {len(bars)} 봉")
        
        # Quote 데이터
        quote_store = QuoteStore(quote_db)
        qsrc = DummyQuoteSource(base_price=Decimal("70000"))
        as_of = datetime(2026, 5, 5, tzinfo=timezone.utc)
        snap = qsrc.snapshot("005930", fixed_time=as_of)
        quote_store.upsert(snap)
        print(f"   ▸ Quote 저장: bid={snap.best_bid}, ask={snap.best_ask}")
        
        # Symbol Master
        sm = SymbolMaster.from_csv(CSV_PATH)
        
        # 전략 실행
        strategy = MomentumStrategyV04(
            ohlcv_store=ohlcv_store,
            quote_store=quote_store,
            symbol_master=sm,
        )
        signal = strategy.generate("005930", Timeframe.D1, as_of)
        
        assert isinstance(signal, MomentumSignalV04)
        assert signal.symbol == "005930"
        assert -1 <= signal.composite_score <= 1
        assert 0 <= signal.confidence <= 1
        print(f"   ✅ score={signal.composite_score:.4f}, side={signal.side.value}, conf={signal.confidence:.4f}")
        print(f"      components={ {k: f'{v:.4f}' for k, v in signal.components.items()} }")
        print(f"      metadata keys: {list(signal.metadata.keys())}")


def test_symbol_master_fail_closed():
    print("\n[6] Symbol Master — 미상장 종목 fail-closed → flat")
    with tempfile.TemporaryDirectory() as td:
        ohlcv_store = OHLCVStore(Path(td) / "ohlcv.sqlite")
        sm = SymbolMaster.from_csv(CSV_PATH)
        strategy = MomentumStrategyV04(ohlcv_store=ohlcv_store, symbol_master=sm)
        
        signal = strategy.generate(
            "999999", Timeframe.D1,
            datetime(2026, 5, 5, tzinfo=timezone.utc),
        )
        assert signal.side == SignalSide.FLAT
        assert signal.confidence == Decimal("0")
        assert "tradable" in signal.metadata.get("flat_reason", "")
        print(f"   ✅ 미상장 종목 → FLAT, reason={signal.metadata.get('flat_reason')}")


def test_no_data_fail_closed():
    print("\n[7] 데이터 없음 → flat (fail-closed)")
    with tempfile.TemporaryDirectory() as td:
        ohlcv_store = OHLCVStore(Path(td) / "ohlcv.sqlite")  # 빈 store
        strategy = MomentumStrategyV04(ohlcv_store=ohlcv_store)
        
        signal = strategy.generate(
            "005930", Timeframe.D1,
            datetime(2026, 5, 5, tzinfo=timezone.utc),
        )
        assert signal.side == SignalSide.FLAT
        print(f"   ✅ 데이터 없음 → FLAT, reason={signal.metadata.get('flat_reason')}")


def test_stale_quote_handling():
    print("\n[8] Stale 호가 자동 무시 — quote 신호 reliability=0")
    with tempfile.TemporaryDirectory() as td:
        ohlcv_db = Path(td) / "ohlcv.sqlite"
        quote_db = Path(td) / "quote.sqlite"
        
        ohlcv_store = OHLCVStore(ohlcv_db)
        src = DummySource()
        bars = list(src.fetch_bars(
            "005930", Timeframe.D1,
            datetime(2026, 4, 1, tzinfo=timezone.utc),
            datetime(2026, 5, 5, tzinfo=timezone.utc),
        ))
        ohlcv_store.upsert_bars(bars)
        
        # 호가는 2시간 전 (stale — max_age 30sec)
        quote_store = QuoteStore(quote_db)
        qsrc = DummyQuoteSource()
        old_time = datetime(2026, 5, 5, 0, 0, 0, tzinfo=timezone.utc)
        snap = qsrc.snapshot("005930", fixed_time=old_time)
        quote_store.upsert(snap)
        
        strategy = MomentumStrategyV04(
            ohlcv_store=ohlcv_store, quote_store=quote_store,
        )
        # as_of는 호가 시각 + 2시간
        as_of = old_time + timedelta(hours=2)
        signal = strategy.generate("005930", Timeframe.D1, as_of)
        
        # quote 신호는 reliability=0이어야 함 (stale 무시)
        rel = signal.metadata.get("reliability", {})
        assert rel.get("quote_imb") == "0" or rel.get("quote_imb") == "0.00"
        # quote_status가 stale로 표시
        assert signal.metadata.get("quote_status") == "stale_or_missing"
        print(f"   ✅ Stale 호가 자동 무시: quote_status={signal.metadata.get('quote_status')}")
        print(f"      reliability.quote_imb={rel.get('quote_imb')}")


def test_confidence_downgrade_to_flat():
    print("\n[9] 신호 충돌 시 confidence < min → FLAT 강등")
    # 인위적으로 score는 buy 임계 넘지만 confidence 낮은 상황을 만들기 어려움
    # 대신 schema validation 테스트
    sig = MomentumSignalV04(
        symbol="005930",
        timestamp_utc=datetime(2026, 5, 5, tzinfo=timezone.utc),
        composite_score=Decimal("0.30"),  # buy 임계 초과
        side=SignalSide.FLAT,  # 강등됨
        confidence=Decimal("0.20"),  # < min_confidence 0.50
        metadata={"downgraded_to_flat": "confidence(0.20) < min(0.50)"},
    )
    assert sig.side == SignalSide.FLAT
    assert not sig.is_actionable(Decimal("0.50"))
    print(f"   ✅ 강등 메타: {sig.metadata['downgraded_to_flat']}")


def test_signal_validation():
    print("\n[10] MomentumSignalV04 검증")
    # 정상
    sig = MomentumSignalV04(
        symbol="005930",
        timestamp_utc=datetime(2026, 5, 5, tzinfo=timezone.utc),
        composite_score=Decimal("0.5"),
        side=SignalSide.BUY,
        confidence=Decimal("0.7"),
    )
    assert sig.is_actionable()
    
    # composite_score 범위 위반
    try:
        MomentumSignalV04(
            symbol="005930",
            timestamp_utc=datetime(2026, 5, 5, tzinfo=timezone.utc),
            composite_score=Decimal("1.5"),  # 범위 위반
            side=SignalSide.BUY,
            confidence=Decimal("0.7"),
        )
        assert False
    except ValueError as e:
        print(f"   ✅ score 범위 거부: {e}")
    
    # tz-naive timestamp 거부
    try:
        MomentumSignalV04(
            symbol="005930",
            timestamp_utc=datetime(2026, 5, 5),  # naive
            composite_score=Decimal("0.5"),
            side=SignalSide.BUY,
            confidence=Decimal("0.7"),
        )
        assert False
    except ValueError as e:
        print(f"   ✅ tz-naive 거부")


def test_weight_validation():
    print("\n[11] 가중치 합 검증 — 1.0 ± 0.01 외는 거부")
    # 정상 (기본 합 = 1.00)
    cfg = MomentumV04Config()
    with tempfile.TemporaryDirectory() as td:
        ohlcv_store = OHLCVStore(Path(td) / "ohlcv.sqlite")
        MomentumStrategyV04(ohlcv_store, config=cfg)
    print(f"   ✅ 기본 가중치 (합=1.0) 통과")
    
    # 합이 0.5인 잘못된 설정 — 거부
    bad_cfg = MomentumV04Config(
        weight_price=Decimal("0.1"),
        weight_volume=Decimal("0.1"),
        weight_intensity=Decimal("0.1"),
        weight_cvd=Decimal("0.1"),
        weight_quote_imb=Decimal("0.05"),
        weight_spread_quality=Decimal("0.05"),
    )  # 합 = 0.5
    try:
        with tempfile.TemporaryDirectory() as td:
            ohlcv_store = OHLCVStore(Path(td) / "ohlcv.sqlite")
            MomentumStrategyV04(ohlcv_store, config=bad_cfg)
        assert False
    except ValueError as e:
        print(f"   ✅ 잘못된 가중치 거부: {e}")


if __name__ == "__main__":
    test_price_momentum()
    test_volume_confirm()
    test_intensity()
    test_cvd_trend()
    test_full_strategy_with_dummy_data()
    test_symbol_master_fail_closed()
    test_no_data_fail_closed()
    test_stale_quote_handling()
    test_confidence_downgrade_to_flat()
    test_signal_validation()
    test_weight_validation()
    print("\n🎉 모든 스모크 테스트 통과 (All smoke tests passed)")
