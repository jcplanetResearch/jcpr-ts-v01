"""스모크 테스트 (Smoke Test) — Task 26 v0.1 P&L Engine."""

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.data.dummy_quote_source import DummyQuoteSource
from src.data.ohlcv_schema import OHLCVBar, Timeframe
from src.data.ohlcv_store import OHLCVStore
from src.data.quote_store import QuoteStore
from src.execution.fills import Fill, FillSide
from src.pnl.pnl_engine import PnLEngine
from src.pnl.pnl_schema import PortfolioPnL, SymbolPnL
from src.pnl.position_ledger import PositionLedger
from src.pnl.position_store import PositionStore


def _make_fill(*, fill_id, side, quantity, price, fee_krw="0", tax_krw="0",
               symbol="005930", filled_at=None):
    return Fill(
        fill_id=fill_id,
        broker_order_no=f"ORD-{fill_id}",
        client_order_id=f"exec-{fill_id}",
        symbol=symbol, side=side, quantity=quantity,
        price=Decimal(price),
        fee_krw=Decimal(fee_krw),
        tax_krw=Decimal(tax_krw),
        filled_at_utc=filled_at or datetime(2026, 5, 6, 9, 0, 0, tzinfo=timezone.utc),
        received_at_utc=datetime.now(timezone.utc),
        source="test",
    )


def _make_ohlcv_bar(symbol, close, *, time=None):
    t = time or datetime(2026, 5, 6, tzinfo=timezone.utc)
    p = Decimal(close)
    return OHLCVBar(
        symbol=symbol, timeframe=Timeframe.D1,
        bar_time_utc=t,
        open=p, high=p, low=p, close=p,
        volume=10000, source="test",
    )


def _build_setup(*, with_quote=False):
    """공통 설정: ledger + ohlcv + (옵션) quote."""
    pos_db = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False).name
    ohlcv_db = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False).name
    quote_db = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False).name

    ledger = PositionLedger(PositionStore(pos_db))
    ohlcv = OHLCVStore(ohlcv_db)
    quote = QuoteStore(quote_db) if with_quote else None
    return ledger, ohlcv, quote, [pos_db, ohlcv_db, quote_db]


def _cleanup(paths):
    for p in paths:
        try:
            Path(p).unlink()
        except FileNotFoundError:
            pass


# ─────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────

def test_symbol_pnl_no_position():
    print("\n[1] 보유 없음 — 빈 결과")
    ledger, ohlcv, _, paths = _build_setup()
    try:
        engine = PnLEngine(ledger, ohlcv)
        pnl = engine.compute_symbol_pnl("005930", as_of_utc=datetime.now(timezone.utc))
        assert pnl.quantity == 0
        assert pnl.realized_pnl_krw == Decimal("0")
        assert pnl.unrealized_pnl_krw is None
        assert pnl.price_source == "none"
        print(f"   ✅ 빈 포지션: {pnl.quantity}주, source={pnl.price_source}")
    finally:
        _cleanup(paths)


def test_symbol_pnl_with_ohlcv_only():
    print("\n[2] OHLCV만 — 미실현 P&L 계산")
    ledger, ohlcv, _, paths = _build_setup()
    try:
        # 매수 10@70000
        ledger.apply_fill(_make_fill(
            fill_id="F1", side=FillSide.BUY, quantity=10, price="70000",
        ))
        # 시세 75000
        ohlcv.upsert_bars([_make_ohlcv_bar("005930", "75000")])

        engine = PnLEngine(ledger, ohlcv)
        pnl = engine.compute_symbol_pnl("005930", as_of_utc=datetime(2026, 5, 6, 9, 30, tzinfo=timezone.utc))

        assert pnl.quantity == 10
        assert pnl.current_price_krw == Decimal("75000")
        assert pnl.price_source == "ohlcv"
        assert pnl.market_value_krw == Decimal("750000")
        # unrealized = (75000 - 70000) * 10 = 50000
        assert pnl.unrealized_pnl_krw == Decimal("50000")
        # total = 0 (realized) + 50000 = 50000
        assert pnl.total_pnl_krw == Decimal("50000")
        print(f"   ✅ unrealized=50000, source=ohlcv")
    finally:
        _cleanup(paths)


def test_symbol_pnl_quote_priority():
    print("\n[3] Quote 우선 — OHLCV 무시")
    ledger, ohlcv, quote, paths = _build_setup(with_quote=True)
    try:
        # 매수 10@70000
        ledger.apply_fill(_make_fill(
            fill_id="F1", side=FillSide.BUY, quantity=10, price="70000",
        ))
        # OHLCV 종가 75000
        ohlcv.upsert_bars([_make_ohlcv_bar("005930", "75000")])
        # Quote: bid 76400, ask 76500 → mid 76450
        as_of = datetime(2026, 5, 6, 14, tzinfo=timezone.utc)
        qsrc = DummyQuoteSource(base_price=Decimal("76400"))
        snap = qsrc.snapshot("005930", fixed_time=as_of)
        quote.upsert(snap)

        engine = PnLEngine(ledger, ohlcv, quote_store=quote)
        pnl = engine.compute_symbol_pnl("005930", as_of_utc=as_of + timedelta(seconds=5))

        assert pnl.price_source == "quote"
        # mid는 quote에서 결정 — DummyQuoteSource의 결정론적 값
        # 정확한 값보다는 source 확인
        assert pnl.current_price_krw is not None
        assert pnl.current_price_krw != Decimal("75000")  # OHLCV가 아님
        print(f"   ✅ price_source=quote, current_price={pnl.current_price_krw}")
    finally:
        _cleanup(paths)


def test_symbol_pnl_stale_quote_falls_back():
    print("\n[4] Stale Quote → OHLCV 폴백")
    ledger, ohlcv, quote, paths = _build_setup(with_quote=True)
    try:
        ledger.apply_fill(_make_fill(
            fill_id="F1", side=FillSide.BUY, quantity=10, price="70000",
        ))
        ohlcv.upsert_bars([_make_ohlcv_bar("005930", "75000")])

        # Quote는 1시간 전 (max_age 30초)
        old_time = datetime(2026, 5, 6, 13, tzinfo=timezone.utc)
        qsrc = DummyQuoteSource()
        quote.upsert(qsrc.snapshot("005930", fixed_time=old_time))

        engine = PnLEngine(ledger, ohlcv, quote_store=quote, max_quote_age_sec=30)
        pnl = engine.compute_symbol_pnl(
            "005930",
            as_of_utc=old_time + timedelta(hours=1),
        )

        assert pnl.price_source == "ohlcv"  # quote stale → ohlcv 폴백
        assert pnl.current_price_krw == Decimal("75000")
        print(f"   ✅ stale quote → ohlcv 폴백 (75000)")
    finally:
        _cleanup(paths)


def test_symbol_pnl_no_price_source():
    print("\n[5] 가격 없음 — unrealized=None")
    ledger, ohlcv, _, paths = _build_setup()
    try:
        ledger.apply_fill(_make_fill(
            fill_id="F1", side=FillSide.BUY, quantity=10, price="70000",
        ))
        # OHLCV 비어있음

        engine = PnLEngine(ledger, ohlcv)
        pnl = engine.compute_symbol_pnl("005930", as_of_utc=datetime.now(timezone.utc))

        assert pnl.quantity == 10
        assert pnl.current_price_krw is None
        assert pnl.unrealized_pnl_krw is None
        assert pnl.total_pnl_krw is None
        assert pnl.price_source == "none"
        print(f"   ✅ unrealized=None, total=None (가격 없음)")
    finally:
        _cleanup(paths)


def test_symbol_pnl_after_full_sell():
    print("\n[6] 전량 매도 후 — qty=0, realized 보존")
    ledger, ohlcv, _, paths = _build_setup()
    try:
        ledger.apply_fill(_make_fill(
            fill_id="F1", side=FillSide.BUY, quantity=10, price="70000",
            filled_at=datetime(2026, 5, 6, 9, tzinfo=timezone.utc),
        ))
        ledger.apply_fill(_make_fill(
            fill_id="F2", side=FillSide.SELL, quantity=10, price="75000",
            tax_krw="1500",
            filled_at=datetime(2026, 5, 6, 10, tzinfo=timezone.utc),
        ))
        # 시세 있어도 qty=0이므로 unrealized=0
        ohlcv.upsert_bars([_make_ohlcv_bar("005930", "80000")])

        engine = PnLEngine(ledger, ohlcv)
        pnl = engine.compute_symbol_pnl("005930", as_of_utc=datetime.now(timezone.utc))

        assert pnl.quantity == 0
        assert pnl.unrealized_pnl_krw == Decimal("0")
        # realized = 10 * (75000 - 70000) - 0 - 1500 = 48500
        assert pnl.realized_pnl_krw == Decimal("48500")
        assert pnl.total_pnl_krw == Decimal("48500")
        print(f"   ✅ qty=0, realized=48500, unrealized=0")
    finally:
        _cleanup(paths)


def test_portfolio_pnl_basic():
    print("\n[7] PortfolioPnL — 종합")
    ledger, ohlcv, _, paths = _build_setup()
    try:
        # 005930: 매수 10@70000, 시세 75000 → unrealized 50000
        ledger.apply_fill(_make_fill(
            fill_id="F1", side=FillSide.BUY, quantity=10, price="70000",
            filled_at=datetime(2026, 5, 6, 9, tzinfo=timezone.utc),
        ))
        # 000660: 매수 후 매도 — realized 발생
        ledger.apply_fill(_make_fill(
            fill_id="F2", side=FillSide.BUY, quantity=10, price="80000", symbol="000660",
            filled_at=datetime(2026, 5, 6, 10, tzinfo=timezone.utc),
        ))
        ledger.apply_fill(_make_fill(
            fill_id="F3", side=FillSide.SELL, quantity=10, price="82000", symbol="000660",
            tax_krw="1500",
            filled_at=datetime(2026, 5, 6, 11, tzinfo=timezone.utc),
        ))

        ohlcv.upsert_bars([
            _make_ohlcv_bar("005930", "75000"),
        ])

        engine = PnLEngine(ledger, ohlcv)
        port = engine.compute_portfolio_pnl(
            starting_capital_krw=Decimal("10000000"),
            cash_krw=Decimal("9000000"),
            as_of_utc=datetime.now(timezone.utc),
        )

        assert isinstance(port, PortfolioPnL)
        assert port.starting_capital_krw == Decimal("10000000")
        # realized: 005930=0, 000660=10*(82000-80000) - 1500 = 18500
        assert port.total_realized_pnl_krw == Decimal("18500")
        # unrealized: 005930=50000, 000660=0 (qty=0)
        assert port.total_unrealized_pnl_krw == Decimal("50000")
        # market value: 005930=10*75000=750000, 000660=0
        assert port.total_market_value_krw == Decimal("750000")
        # ending = 9000000 + 750000 = 9750000
        assert port.ending_capital_krw == Decimal("9750000")
        # taxes
        assert port.total_taxes_krw == Decimal("1500")

        # by_symbol
        assert port.by_symbol_realized_krw["000660"] == Decimal("18500")
        assert port.by_symbol_realized_krw["005930"] == Decimal("0")
        assert port.by_symbol_unrealized_krw["005930"] == Decimal("50000")
        assert port.by_symbol_unrealized_krw["000660"] == Decimal("0")

        print(f"   ✅ realized=18500, unrealized=50000, ending={port.ending_capital_krw}")
    finally:
        _cleanup(paths)


def test_portfolio_pnl_stale_symbols():
    print("\n[8] PortfolioPnL — stale 종목 표기")
    ledger, ohlcv, _, paths = _build_setup()
    try:
        # 005930: OHLCV 있음
        ledger.apply_fill(_make_fill(
            fill_id="F1", side=FillSide.BUY, quantity=10, price="70000",
        ))
        ohlcv.upsert_bars([_make_ohlcv_bar("005930", "75000")])

        # 000660: 매수만, OHLCV 없음 → stale
        ledger.apply_fill(_make_fill(
            fill_id="F2", side=FillSide.BUY, quantity=5, price="80000", symbol="000660",
            filled_at=datetime(2026, 5, 6, 10, tzinfo=timezone.utc),
        ))

        engine = PnLEngine(ledger, ohlcv)
        port = engine.compute_portfolio_pnl(
            starting_capital_krw=Decimal("10000000"),
            cash_krw=Decimal("9000000"),
            as_of_utc=datetime.now(timezone.utc),
        )

        assert "000660" in port.stale_symbols
        assert "005930" not in port.stale_symbols
        # 000660은 unrealized 합계에서 제외
        assert port.total_unrealized_pnl_krw == Decimal("50000")
        print(f"   ✅ stale_symbols={port.stale_symbols}")
    finally:
        _cleanup(paths)


def test_portfolio_pnl_to_summary_dict():
    print("\n[9] to_summary_dict — Final Output 형식")
    ledger, ohlcv, _, paths = _build_setup()
    try:
        ledger.apply_fill(_make_fill(
            fill_id="F1", side=FillSide.BUY, quantity=10, price="70000",
        ))
        ohlcv.upsert_bars([_make_ohlcv_bar("005930", "75000")])

        engine = PnLEngine(ledger, ohlcv)
        port = engine.compute_portfolio_pnl(
            starting_capital_krw=Decimal("10000000"),
            cash_krw=Decimal("9300000"),
            as_of_utc=datetime.now(timezone.utc),
        )
        d = port.to_summary_dict()

        # Final output 키 확인
        assert "starting_capital_krw" in d         # #1
        assert "ending_capital_krw" in d           # #2
        assert "realized_pnl_krw" in d             # #3
        assert "unrealized_pnl_krw" in d           # #4
        assert "fees_krw" in d and "taxes_krw" in d  # #5
        assert "strategy_attribution" in d         # #6
        assert "symbol_attribution" in d           # #7

        # 기본 무결성
        assert d["starting_capital_krw"] == "10000000"
        assert d["unrealized_pnl_krw"] == "50000"

        # JSON 직렬화 가능 확인
        import json
        json_str = json.dumps(d, ensure_ascii=False)
        assert "10000000" in json_str
        print(f"   ✅ Final output 키 {list(d.keys())[:5]}...")
        print(f"   ✅ summary 직렬화 OK")
    finally:
        _cleanup(paths)


def test_portfolio_return_pct():
    print("\n[10] return_pct 계산")
    ledger, ohlcv, _, paths = _build_setup()
    try:
        ledger.apply_fill(_make_fill(
            fill_id="F1", side=FillSide.BUY, quantity=10, price="70000",
        ))
        ohlcv.upsert_bars([_make_ohlcv_bar("005930", "77000")])

        engine = PnLEngine(ledger, ohlcv)
        port = engine.compute_portfolio_pnl(
            starting_capital_krw=Decimal("1000000"),
            cash_krw=Decimal("0"),
            as_of_utc=datetime.now(timezone.utc),
        )
        # unrealized = (77000-70000)*10 = 70000
        # total_pnl = 0 + 70000 = 70000
        # return = 70000/1000000 = 0.07 (7%)
        assert port.total_pnl_krw() == Decimal("70000")
        assert port.return_pct() == Decimal("0.07")
        print(f"   ✅ return={port.return_pct():.2%}")
    finally:
        _cleanup(paths)


def test_portfolio_strategy_attribution():
    print("\n[11] Strategy attribution (v0.1 단일)")
    ledger, ohlcv, _, paths = _build_setup()
    try:
        ledger.apply_fill(_make_fill(
            fill_id="F1", side=FillSide.BUY, quantity=10, price="70000",
        ))
        ledger.apply_fill(_make_fill(
            fill_id="F2", side=FillSide.SELL, quantity=10, price="72000", tax_krw="1500",
            filled_at=datetime(2026, 5, 6, 10, tzinfo=timezone.utc),
        ))

        engine = PnLEngine(ledger, ohlcv, default_strategy_id="momentum_v04")
        port = engine.compute_portfolio_pnl(
            starting_capital_krw=Decimal("10000000"),
            cash_krw=Decimal("10000000"),
            as_of_utc=datetime.now(timezone.utc),
        )

        assert len(port.by_strategy) == 1
        sa = port.by_strategy[0]
        assert sa.strategy_id == "momentum_v04"
        assert sa.fills_count == 2
        # realized = 10*(72000-70000) - 1500 = 18500
        assert sa.realized_pnl_krw == Decimal("18500")
        print(f"   ✅ strategy={sa.strategy_id}, fills={sa.fills_count}, realized={sa.realized_pnl_krw}")
    finally:
        _cleanup(paths)


def test_invalid_inputs():
    print("\n[12] 잘못된 입력 거부")
    ledger, ohlcv, _, paths = _build_setup()
    try:
        engine = PnLEngine(ledger, ohlcv)

        # tz-naive
        try:
            engine.compute_symbol_pnl("005930", as_of_utc=datetime.now())
            assert False
        except ValueError as e:
            assert "tz-aware" in str(e)
            print(f"   ✅ tz-naive 거부")

        # 음수 starting_capital
        try:
            engine.compute_portfolio_pnl(
                starting_capital_krw=Decimal("-100"),
                cash_krw=Decimal("0"),
                as_of_utc=datetime.now(timezone.utc),
            )
            assert False
        except ValueError as e:
            print(f"   ✅ 음수 starting_capital 거부")
    finally:
        _cleanup(paths)


if __name__ == "__main__":
    test_symbol_pnl_no_position()
    test_symbol_pnl_with_ohlcv_only()
    test_symbol_pnl_quote_priority()
    test_symbol_pnl_stale_quote_falls_back()
    test_symbol_pnl_no_price_source()
    test_symbol_pnl_after_full_sell()
    test_portfolio_pnl_basic()
    test_portfolio_pnl_stale_symbols()
    test_portfolio_pnl_to_summary_dict()
    test_portfolio_return_pct()
    test_portfolio_strategy_attribution()
    test_invalid_inputs()
    print("\n🎉 모든 스모크 테스트 통과 (All smoke tests passed)")
