"""스모크 테스트 (Smoke Test) — Task 12 v0.1 OHLCV Ingestion."""

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.data.ohlcv_schema import OHLCVBar, Timeframe, TickDirection, VolumeSplitMethod
from src.data.volume_classifier import (
    classify_tick_direction, classify_bar,
    estimate_up_down_volume_hybrid, estimate_up_down_volume_simple,
)
from src.data.dummy_source import DummySource
from src.data.ohlcv_store import OHLCVStore
from src.data.market_data import MarketDataIngester
from src.data.symbol_master import SymbolMaster


CSV_PATH = Path(__file__).parent / "data" / "reference" / "symbol_master.csv"


def test_ohlcv_bar_validation():
    print("\n[1] OHLCVBar 검증 (validation)")
    # 정상 봉
    b = OHLCVBar(
        symbol="005930", timeframe=Timeframe.D1,
        bar_time_utc=datetime(2026, 5, 6, tzinfo=timezone.utc),
        open=Decimal("70000"), high=Decimal("71000"),
        low=Decimal("69500"), close=Decimal("70500"),
        volume=10000,
    )
    assert b.intra_bar_pressure() == (Decimal("70500") - Decimal("69500")) / (Decimal("71000") - Decimal("69500"))
    print("   ✅ 정상 봉 생성")

    # tz-naive 거부
    try:
        OHLCVBar(
            symbol="005930", timeframe=Timeframe.D1,
            bar_time_utc=datetime(2026, 5, 6),  # naive!
            open=Decimal("70000"), high=Decimal("71000"),
            low=Decimal("69500"), close=Decimal("70500"),
            volume=10000,
        )
        assert False
    except ValueError as e:
        print(f"   ✅ tz-naive 거부: {e}")

    # OHLC 정합성 위반
    try:
        OHLCVBar(
            symbol="005930", timeframe=Timeframe.D1,
            bar_time_utc=datetime(2026, 5, 6, tzinfo=timezone.utc),
            open=Decimal("70000"), high=Decimal("69000"),  # high < open!
            low=Decimal("68500"), close=Decimal("69500"),
            volume=10000,
        )
        assert False
    except ValueError as e:
        print(f"   ✅ OHLC 정합성 거부")

    # up + down > volume 거부
    try:
        OHLCVBar(
            symbol="005930", timeframe=Timeframe.D1,
            bar_time_utc=datetime(2026, 5, 6, tzinfo=timezone.utc),
            open=Decimal("70000"), high=Decimal("71000"),
            low=Decimal("69500"), close=Decimal("70500"),
            volume=10000,
            up_volume=8000, down_volume=3000,  # 합 11000 > 10000
        )
        assert False
    except ValueError as e:
        print(f"   ✅ up+down>volume 거부")


def test_classify_tick_direction():
    print("\n[2] tick_direction 분류")
    assert classify_tick_direction(Decimal("100"), Decimal("99")) == TickDirection.UP
    assert classify_tick_direction(Decimal("99"), Decimal("100")) == TickDirection.DOWN
    assert classify_tick_direction(Decimal("100"), Decimal("100")) == TickDirection.ZERO
    assert classify_tick_direction(Decimal("100"), None) == TickDirection.UNKNOWN
    print("   ✅ UP/DOWN/ZERO/UNKNOWN 모두 정확")


def test_volume_classifier_hybrid():
    print("\n[3] 하이브리드 추정")
    # 상승봉 + 종가가 고점 근처 → 매수 강도 높아야 함
    up, down, method = estimate_up_down_volume_hybrid(
        open_=Decimal("100"), high=Decimal("105"),
        low=Decimal("99"), close=Decimal("104"),
        volume=10000, prev_close=Decimal("100"),
    )
    assert method == VolumeSplitMethod.ESTIMATED_HYBRID
    assert up + down == 10000
    assert up > down, f"상승봉인데 up({up}) <= down({down})"
    print(f"   ✅ 상승봉: up={up}, down={down}, method={method.value}")

    # 하락봉 + 종가가 저점 근처
    up, down, method = estimate_up_down_volume_hybrid(
        open_=Decimal("100"), high=Decimal("101"),
        low=Decimal("95"), close=Decimal("96"),
        volume=10000, prev_close=Decimal("100"),
    )
    assert up + down == 10000
    assert down > up
    print(f"   ✅ 하락봉: up={up}, down={down}")

    # prev_close 없음 → ESTIMATED_INTRABAR로 변경
    up, down, method = estimate_up_down_volume_hybrid(
        open_=Decimal("100"), high=Decimal("105"),
        low=Decimal("99"), close=Decimal("104"),
        volume=10000, prev_close=None,
    )
    assert method == VolumeSplitMethod.ESTIMATED_INTRABAR
    print(f"   ✅ prev_close 없을 때 intrabar fallback")

    # volume == 0
    up, down, method = estimate_up_down_volume_hybrid(
        open_=Decimal("100"), high=Decimal("105"),
        low=Decimal("99"), close=Decimal("104"),
        volume=0, prev_close=Decimal("100"),
    )
    assert up == 0 and down == 0
    print(f"   ✅ volume=0 처리")


def test_dummy_source():
    print("\n[4] DummySource — 합성 데이터 생성")
    src = DummySource()
    assert src.is_live is False  # 실거래 차단

    bars = list(src.fetch_bars(
        "005930", Timeframe.D1,
        datetime(2026, 5, 1, tzinfo=timezone.utc),
        datetime(2026, 5, 5, tzinfo=timezone.utc),
    ))
    assert len(bars) == 5
    # 첫 봉은 UNKNOWN 또는 INTRABAR (prev_close 없음)
    first = bars[0]
    assert first.tick_direction == TickDirection.UNKNOWN
    assert first.volume_split_method == VolumeSplitMethod.ESTIMATED_INTRABAR
    # 둘째 봉부터는 분류됨
    second = bars[1]
    assert second.tick_direction in (TickDirection.UP, TickDirection.DOWN, TickDirection.ZERO)
    assert second.volume_split_method == VolumeSplitMethod.ESTIMATED_HYBRID
    assert second.up_volume + second.down_volume == second.volume

    # 결정론 (같은 입력 → 같은 출력)
    bars2 = list(src.fetch_bars(
        "005930", Timeframe.D1,
        datetime(2026, 5, 1, tzinfo=timezone.utc),
        datetime(2026, 5, 5, tzinfo=timezone.utc),
    ))
    assert bars[0].close == bars2[0].close
    print(f"   ✅ {len(bars)}개 봉 생성, is_live=False, 결정론 OK")


def test_store_roundtrip():
    print("\n[5] OHLCVStore — upsert + fetch 왕복")
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db_path = f.name

    try:
        store = OHLCVStore(db_path)
        src = DummySource()
        bars = list(src.fetch_bars(
            "005930", Timeframe.D1,
            datetime(2026, 5, 1, tzinfo=timezone.utc),
            datetime(2026, 5, 5, tzinfo=timezone.utc),
        ))
        n = store.upsert_bars(bars)
        assert n == len(bars)

        fetched = store.fetch(
            "005930", Timeframe.D1,
            datetime(2026, 5, 1, tzinfo=timezone.utc),
            datetime(2026, 5, 5, tzinfo=timezone.utc),
        )
        assert len(fetched) == len(bars)
        assert fetched[0].close == bars[0].close
        assert fetched[1].tick_direction == bars[1].tick_direction
        assert fetched[1].tick_direction_alt == bars[1].tick_direction_alt
        # 멱등 upsert (같은 봉 재입력 → 같은 행 수)
        store.upsert_bars(bars)
        fetched2 = store.fetch(
            "005930", Timeframe.D1,
            datetime(2026, 5, 1, tzinfo=timezone.utc),
            datetime(2026, 5, 5, tzinfo=timezone.utc),
        )
        assert len(fetched2) == len(bars)  # 중복 안 됨
        print(f"   ✅ {n}개 저장, 왕복 일치, 멱등성 OK")
    finally:
        Path(db_path).unlink()


def test_intensity_and_cvd():
    print("\n[6] 매수/매도 강도 + CVD 조회")
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db_path = f.name

    try:
        store = OHLCVStore(db_path)
        src = DummySource()
        bars = list(src.fetch_bars(
            "005930", Timeframe.D1,
            datetime(2026, 5, 1, tzinfo=timezone.utc),
            datetime(2026, 5, 10, tzinfo=timezone.utc),
        ))
        store.upsert_bars(bars)

        # 강도
        intensity_series = store.fetch_buy_sell_intensity(
            "005930", Timeframe.D1,
            datetime(2026, 5, 1, tzinfo=timezone.utc),
            datetime(2026, 5, 10, tzinfo=timezone.utc),
        )
        assert len(intensity_series) == len(bars)
        # 모든 강도는 None 또는 [0,1]
        for t, intensity, method in intensity_series:
            if intensity is not None:
                assert Decimal("0") <= intensity <= Decimal("1"), f"강도 범위: {intensity}"
        print(f"   ✅ Intensity 시리즈 {len(intensity_series)}개")

        # CVD
        cvd_series = store.fetch_cumulative_volume_delta(
            "005930", Timeframe.D1,
            datetime(2026, 5, 1, tzinfo=timezone.utc),
            datetime(2026, 5, 10, tzinfo=timezone.utc),
        )
        assert len(cvd_series) == len(bars)
        # 마지막 CVD = 모든 (up - down) 합
        expected_cvd = sum(
            (b.up_volume - b.down_volume) if b.up_volume is not None and b.down_volume is not None else 0
            for b in bars
        )
        assert cvd_series[-1][1] == expected_cvd
        print(f"   ✅ CVD 시리즈 {len(cvd_series)}개, 최종 CVD={cvd_series[-1][1]}")
    finally:
        Path(db_path).unlink()


def test_ingester_with_symbol_master():
    print("\n[7] MarketDataIngester + SymbolMaster 통합")
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db_path = f.name

    try:
        store = OHLCVStore(db_path)
        src = DummySource()
        sm = SymbolMaster.from_csv(CSV_PATH)
        ingester = MarketDataIngester(src, store, symbol_master=sm)

        # 정상 종목
        report = ingester.ingest(
            "005930", Timeframe.D1,
            datetime(2026, 5, 1, tzinfo=timezone.utc),
            datetime(2026, 5, 5, tzinfo=timezone.utc),
        )
        assert report.error is None
        assert report.bars_stored == 5
        assert report.is_live_source is False
        print(f"   ✅ 정상 종목: {report.bars_stored} 저장")

        # 알 수 없는 종목 — fail-closed
        report2 = ingester.ingest(
            "999999", Timeframe.D1,
            datetime(2026, 5, 1, tzinfo=timezone.utc),
            datetime(2026, 5, 5, tzinfo=timezone.utc),
        )
        assert report2.error is not None
        assert report2.bars_stored == 0
        print(f"   ✅ 미상장 종목 거부: {report2.error}")
    finally:
        Path(db_path).unlink()


def test_require_live_source():
    print("\n[8] require_live_source — DummySource 거부")
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db_path = f.name

    try:
        store = OHLCVStore(db_path)
        src = DummySource()
        try:
            MarketDataIngester(src, store, require_live_source=True)
            assert False, "RuntimeError 발생해야 함"
        except RuntimeError as e:
            print(f"   ✅ DummySource 거부 (실거래 모드): {e}")
    finally:
        Path(db_path).unlink()


def test_tick_direction_alt():
    print("\n[9] tick_direction_alt 필드 — 대체 출처 보존")
    bar = OHLCVBar(
        symbol="005930", timeframe=Timeframe.D1,
        bar_time_utc=datetime(2026, 5, 6, tzinfo=timezone.utc),
        open=Decimal("70000"), high=Decimal("71000"),
        low=Decimal("69500"), close=Decimal("70500"),
        volume=10000,
        tick_direction=TickDirection.UP,
        tick_direction_alt=TickDirection.DOWN,  # 의도적으로 다르게 (alt 출처 다름)
    )
    assert bar.tick_direction == TickDirection.UP
    assert bar.tick_direction_alt == TickDirection.DOWN
    # 저장/조회 왕복에서도 보존되는지
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db_path = f.name
    try:
        store = OHLCVStore(db_path)
        store.upsert_bars([bar])
        fetched = store.fetch(
            "005930", Timeframe.D1,
            datetime(2026, 5, 6, tzinfo=timezone.utc),
            datetime(2026, 5, 6, tzinfo=timezone.utc),
        )
        assert len(fetched) == 1
        assert fetched[0].tick_direction == TickDirection.UP
        assert fetched[0].tick_direction_alt == TickDirection.DOWN
        print(f"   ✅ tick_direction={fetched[0].tick_direction.value}, alt={fetched[0].tick_direction_alt.value}")
    finally:
        Path(db_path).unlink()


if __name__ == "__main__":
    test_ohlcv_bar_validation()
    test_classify_tick_direction()
    test_volume_classifier_hybrid()
    test_dummy_source()
    test_store_roundtrip()
    test_intensity_and_cvd()
    test_ingester_with_symbol_master()
    test_require_live_source()
    test_tick_direction_alt()
    print("\n🎉 모든 스모크 테스트 통과 (All smoke tests passed)")
