"""스모크 테스트 (Smoke Test) — Task 10 v0.1 Symbol Master."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.data.symbol_master import SymbolMaster, SymbolValidationError
from src.data.symbol_status import InstrumentType, Market, SymbolStatus


CSV_PATH = Path(__file__).parent / "data" / "reference" / "symbol_master.csv"


def test_load_csv():
    print("\n[1] CSV 로드 (Load CSV)")
    sm = SymbolMaster.from_csv(CSV_PATH)
    assert len(sm) == 10, f"expected 10, got {len(sm)}"
    print(f"   ✅ {len(sm)} 종목 로드됨")
    return sm


def test_query(sm):
    print("\n[2] 단일 조회 (Single lookup)")
    samsung = sm.get("005930")
    assert samsung.name_kr == "삼성전자"
    assert samsung.market == Market.KOSPI
    assert samsung.instrument_type == InstrumentType.STOCK
    assert samsung.is_tradable()
    print(f"   ✅ 005930: {samsung.name_kr} / {samsung.market.value} / tradable={samsung.is_tradable()}")


def test_unknown_code(sm):
    print("\n[3] 알 수 없는 코드 - fail-closed")
    try:
        sm.get("999999")
        assert False, "KeyError가 발생해야 함"
    except KeyError as e:
        print(f"   ✅ KeyError 발생 (expected): {e}")

    assert sm.try_get("999999") is None
    assert not sm.is_tradable("999999")  # fail-closed
    assert not sm.exists("999999")
    print(f"   ✅ try_get/is_tradable/exists 모두 안전하게 false 반환")


def test_filters(sm):
    print("\n[4] 필터 조회 (Filter queries)")
    kospi = sm.filter_by_market(Market.KOSPI)
    kosdaq = sm.filter_by_market(Market.KOSDAQ)
    etfs = sm.filter_by_instrument(InstrumentType.ETF)
    active = sm.filter_by_status(SymbolStatus.ACTIVE)

    assert len(kospi) == 8, f"KOSPI count: {len(kospi)}"
    assert len(kosdaq) == 2, f"KOSDAQ count: {len(kosdaq)}"
    assert len(etfs) == 2, f"ETF count: {len(etfs)}"
    assert len(active) == 10
    print(f"   ✅ KOSPI={len(kospi)}, KOSDAQ={len(kosdaq)}, ETF={len(etfs)}, active={len(active)}")


def test_summary(sm):
    print("\n[5] 요약 통계 (Summary)")
    s = sm.summary()
    print(f"   ✅ summary={s}")
    assert s["total"] == 10
    assert s["tradable_count"] == 10


def test_invalid_csv():
    print("\n[6] 잘못된 CSV - fail-closed")
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8") as f:
        f.write("code,name_kr,name_en,market,instrument_type,lot_size,tick_policy,status,currency,listed_date,updated_at\n")
        f.write("INVALID,bad,bad,KOSPI,stock,1,krx_stock,active,KRW,,\n")  # 6자리 숫자가 아님
        bad_path = f.name

    try:
        SymbolMaster.from_csv(bad_path)
        assert False, "검증 실패해야 함"
    except SymbolValidationError as e:
        print(f"   ✅ 검증 실패 (expected): {e}")
    finally:
        Path(bad_path).unlink()


def test_duplicate_code():
    print("\n[7] 중복 코드 - fail-closed")
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8") as f:
        f.write("code,name_kr,name_en,market,instrument_type,lot_size,tick_policy,status,currency,listed_date,updated_at\n")
        f.write("005930,삼성전자,Samsung,KOSPI,stock,1,krx_stock,active,KRW,,\n")
        f.write("005930,중복,Duplicate,KOSPI,stock,1,krx_stock,active,KRW,,\n")
        dup_path = f.name

    try:
        SymbolMaster.from_csv(dup_path)
        assert False, "중복 코드 검증 실패해야 함"
    except SymbolValidationError as e:
        print(f"   ✅ 중복 거부 (expected): {e}")
    finally:
        Path(dup_path).unlink()


def test_sqlite_roundtrip(sm):
    print("\n[8] SQLite 왕복 (CSV → SQLite → reload)")
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db_path = f.name

    try:
        sm.to_sqlite(db_path)
        sm2 = SymbolMaster.from_sqlite(db_path)
        assert len(sm2) == len(sm)
        s1 = sm.get("005930")
        s2 = sm2.get("005930")
        assert s1.name_kr == s2.name_kr
        assert s1.market == s2.market
        assert s1.lot_size == s2.lot_size
        print(f"   ✅ SQLite 왕복 성공: {len(sm2)} 종목")
    finally:
        Path(db_path).unlink()


def test_integration_with_tick_size(sm):
    print("\n[9] Task 18 tick_size와 통합 (Integration with Task 18)")
    from src.execution.tick_size import align_price_to_tick

    samsung = sm.get("005930")
    # InstrumentType enum의 .value를 Literal로 전달
    aligned = align_price_to_tick(
        70150,
        side="buy",
        instrument_type=samsung.instrument_type.value,
        conservative=True,
    )
    assert aligned.aligned_price == 70100
    print(f"   ✅ 005930 ({samsung.instrument_type.value}) tick aligned: 70150 → {aligned.aligned_price}")

    etf = sm.get("069500")
    aligned_etf = align_price_to_tick(
        12347,
        side="buy",
        instrument_type=etf.instrument_type.value,
        conservative=True,
    )
    assert aligned_etf.aligned_price == 12345  # ETF tick=5
    print(f"   ✅ 069500 ({etf.instrument_type.value}) tick aligned: 12347 → {aligned_etf.aligned_price}")


if __name__ == "__main__":
    sm = test_load_csv()
    test_query(sm)
    test_unknown_code(sm)
    test_filters(sm)
    test_summary(sm)
    test_invalid_csv()
    test_duplicate_code()
    test_sqlite_roundtrip(sm)
    test_integration_with_tick_size(sm)
    print("\n🎉 모든 스모크 테스트 통과 (All smoke tests passed)")
