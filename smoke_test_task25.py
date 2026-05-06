"""스모크 테스트 (Smoke Test) — Task 25 v0.1 Position Ledger."""

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.execution.fills import Fill, FillSide
from src.execution.fill_store import FillStore
from src.pnl.position_state import (
    FillApplicationResult, PositionLogicError, PositionState,
    apply_fill_to_state,
)
from src.pnl.position_store import PositionStore
from src.pnl.position_ledger import PositionLedger


# ─────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────

def _make_fill(
    *, fill_id, side, quantity, price,
    fee_krw="0", tax_krw="0",
    symbol="005930", filled_at=None,
):
    return Fill(
        fill_id=fill_id,
        broker_order_no=f"ORD-{fill_id}",
        client_order_id=f"exec-{fill_id}",
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=Decimal(price),
        fee_krw=Decimal(fee_krw),
        tax_krw=Decimal(tax_krw),
        filled_at_utc=filled_at or datetime(2026, 5, 6, 9, 0, 0, tzinfo=timezone.utc),
        received_at_utc=datetime.now(timezone.utc),
        source="test",
    )


# ─────────────────────────────────────────────────
# Tests — PositionState 단위
# ─────────────────────────────────────────────────

def test_empty_position():
    print("\n[1] PositionState.empty()")
    p = PositionState.empty("005930")
    assert p.quantity == 0
    assert p.avg_cost_krw == Decimal("0")
    assert p.realized_pnl_krw == Decimal("0")
    assert not p.is_active()
    print(f"   ✅ 빈 포지션 OK")


def test_position_validation():
    print("\n[2] PositionState 검증")
    # 음수 quantity 거부 (공매도 금지 v0.1)
    try:
        PositionState(
            symbol="005930", quantity=-5,
            avg_cost_krw=Decimal("0"), realized_pnl_krw=Decimal("0"),
            total_fees_krw=Decimal("0"), total_taxes_krw=Decimal("0"),
        )
        assert False
    except ValueError as e:
        assert "공매도" in str(e) or "음수" in str(e)
        print(f"   ✅ 음수 quantity 거부")

    # 음수 avg_cost
    try:
        PositionState(
            symbol="005930", quantity=10,
            avg_cost_krw=Decimal("-100"),
            realized_pnl_krw=Decimal("0"),
            total_fees_krw=Decimal("0"), total_taxes_krw=Decimal("0"),
        )
        assert False
    except ValueError:
        print(f"   ✅ 음수 avg_cost 거부")


# ─────────────────────────────────────────────────
# Tests — apply_fill_to_state (순수 함수)
# ─────────────────────────────────────────────────

def test_buy_to_empty():
    print("\n[3] 매수 (빈 포지션 → 신규)")
    state = PositionState.empty("005930")
    fill = _make_fill(
        fill_id="F1", side=FillSide.BUY, quantity=10, price="70000", fee_krw="200",
    )
    result = apply_fill_to_state(state, fill)
    
    assert result.new_state.quantity == 10
    # avg = (0 * 0 + 10 * 70000 + 200) / 10 = 700200/10 = 70020
    assert result.new_state.avg_cost_krw == Decimal("70020")
    assert result.realized_pnl_delta_krw == Decimal("0")  # 매수는 실현 0
    assert result.new_state.total_fees_krw == Decimal("200")
    print(f"   ✅ qty=10, avg={result.new_state.avg_cost_krw} (수수료 포함)")


def test_buy_average_cost_weighted():
    print("\n[4] 매수 추가 — 가중 평균")
    state = PositionState.empty("005930")
    # 첫 매수: 10주 @ 70000, fee 0
    s1 = apply_fill_to_state(state, _make_fill(
        fill_id="F1", side=FillSide.BUY, quantity=10, price="70000",
    )).new_state
    assert s1.avg_cost_krw == Decimal("70000")
    
    # 둘째 매수: 10주 @ 72000, fee 0
    s2 = apply_fill_to_state(s1, _make_fill(
        fill_id="F2", side=FillSide.BUY, quantity=10, price="72000",
    )).new_state
    # 가중 평균: (10*70000 + 10*72000)/20 = 71000
    assert s2.avg_cost_krw == Decimal("71000")
    assert s2.quantity == 20
    print(f"   ✅ 10@70000 + 10@72000 → avg=71000, qty=20")


def test_sell_partial():
    print("\n[5] 부분 매도 — 평균 유지, 실현 P&L 발생")
    # 보유: 20주 @ avg 71000
    state = PositionState(
        symbol="005930", quantity=20,
        avg_cost_krw=Decimal("71000"), realized_pnl_krw=Decimal("0"),
        total_fees_krw=Decimal("0"), total_taxes_krw=Decimal("0"),
    )
    fill = _make_fill(
        fill_id="F3", side=FillSide.SELL, quantity=5, price="75000",
        fee_krw="100", tax_krw="850",
    )
    result = apply_fill_to_state(state, fill)
    
    # gross = 5 * 75000 = 375000
    # cost_basis = 5 * 71000 = 355000
    # realized = 375000 - 355000 - 100 - 850 = 19050
    assert result.realized_pnl_delta_krw == Decimal("19050")
    assert result.new_state.quantity == 15  # 20 - 5
    assert result.new_state.avg_cost_krw == Decimal("71000")  # 평균 유지
    assert result.new_state.realized_pnl_krw == Decimal("19050")
    assert result.new_state.total_taxes_krw == Decimal("850")
    print(f"   ✅ 5@75000 매도: realized={result.realized_pnl_delta_krw}, qty={result.new_state.quantity}")


def test_sell_full_resets_avg():
    print("\n[6] 전량 매도 — qty=0, avg=0 reset")
    state = PositionState(
        symbol="005930", quantity=10,
        avg_cost_krw=Decimal("70000"), realized_pnl_krw=Decimal("0"),
        total_fees_krw=Decimal("0"), total_taxes_krw=Decimal("0"),
    )
    fill = _make_fill(
        fill_id="F4", side=FillSide.SELL, quantity=10, price="72000",
        fee_krw="0", tax_krw="1500",
    )
    result = apply_fill_to_state(state, fill)
    
    assert result.new_state.quantity == 0
    assert result.new_state.avg_cost_krw == Decimal("0")  # reset!
    # realized = 10*(72000-70000) - 0 - 1500 = 18500
    assert result.realized_pnl_delta_krw == Decimal("18500")
    print(f"   ✅ 전량매도 → qty=0, avg=0, realized={result.realized_pnl_delta_krw}")


def test_oversell_rejected():
    print("\n[7] 매도 > 보유 — 공매도 금지")
    state = PositionState(
        symbol="005930", quantity=10,
        avg_cost_krw=Decimal("70000"), realized_pnl_krw=Decimal("0"),
        total_fees_krw=Decimal("0"), total_taxes_krw=Decimal("0"),
    )
    fill = _make_fill(
        fill_id="F5", side=FillSide.SELL, quantity=15, price="72000",
    )
    try:
        apply_fill_to_state(state, fill)
        assert False
    except PositionLogicError as e:
        assert "공매도" in str(e) or "보유" in str(e)
        print(f"   ✅ 매도>보유 거부: {e}")


def test_symbol_mismatch():
    print("\n[8] 종목 불일치 거부")
    state = PositionState.empty("005930")
    fill = _make_fill(fill_id="F6", side=FillSide.BUY, quantity=10, price="70000",
                      symbol="000660")  # 다른 종목
    try:
        apply_fill_to_state(state, fill)
        assert False
    except PositionLogicError as e:
        assert "symbol" in str(e).lower() or "종목" in str(e)
        print(f"   ✅ 종목 불일치 거부")


# ─────────────────────────────────────────────────
# Tests — PositionStore
# ─────────────────────────────────────────────────

def test_store_roundtrip():
    print("\n[9] PositionStore 왕복")
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db_path = f.name
    try:
        store = PositionStore(db_path)
        state = PositionState(
            symbol="005930", quantity=10,
            avg_cost_krw=Decimal("70020.5"),  # 소수점 정밀도 검증
            realized_pnl_krw=Decimal("19050"),
            total_fees_krw=Decimal("300"),
            total_taxes_krw=Decimal("850"),
            last_updated_utc=datetime(2026, 5, 6, 9, 0, 0, tzinfo=timezone.utc),
            fills_processed=2,
        )
        store.upsert(state)
        
        loaded = store.get("005930")
        assert loaded is not None
        assert loaded.quantity == 10
        assert loaded.avg_cost_krw == Decimal("70020.5")  # 정밀도 보존
        assert loaded.realized_pnl_krw == Decimal("19050")
        print(f"   ✅ 정밀도 보존: avg={loaded.avg_cost_krw}")
    finally:
        Path(db_path).unlink()


def test_store_get_all_active_only():
    print("\n[10] get_all only_active 필터")
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db_path = f.name
    try:
        store = PositionStore(db_path)
        active = PositionState(
            symbol="005930", quantity=10,
            avg_cost_krw=Decimal("70000"), realized_pnl_krw=Decimal("0"),
            total_fees_krw=Decimal("0"), total_taxes_krw=Decimal("0"),
        )
        closed = PositionState(
            symbol="000660", quantity=0,
            avg_cost_krw=Decimal("0"), realized_pnl_krw=Decimal("5000"),
            total_fees_krw=Decimal("100"), total_taxes_krw=Decimal("200"),
        )
        store.upsert(active)
        store.upsert(closed)
        
        active_only = store.get_all(only_active=True)
        assert "005930" in active_only
        assert "000660" not in active_only
        
        all_pos = store.get_all(only_active=False)
        assert len(all_pos) == 2
        print(f"   ✅ active=1, all=2")
    finally:
        Path(db_path).unlink()


# ─────────────────────────────────────────────────
# Tests — PositionLedger 통합
# ─────────────────────────────────────────────────

def test_ledger_apply_single():
    print("\n[11] PositionLedger.apply_fill")
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db_path = f.name
    try:
        ledger = PositionLedger(PositionStore(db_path))
        fill = _make_fill(
            fill_id="F1", side=FillSide.BUY, quantity=10, price="70000", fee_krw="200",
        )
        result = ledger.apply_fill(fill)
        assert result.new_state.quantity == 10
        
        # 저장 확인
        loaded = ledger.get("005930")
        assert loaded.quantity == 10
        print(f"   ✅ apply + 저장 확인")
    finally:
        Path(db_path).unlink()


def test_ledger_apply_fills_sorted():
    print("\n[12] apply_fills — 자동 시간 정렬")
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db_path = f.name
    try:
        ledger = PositionLedger(PositionStore(db_path))
        # 의도적으로 역순 입력
        fills = [
            _make_fill(fill_id="F2", side=FillSide.BUY, quantity=10, price="72000",
                       filled_at=datetime(2026, 5, 6, 10, 0, 0, tzinfo=timezone.utc)),
            _make_fill(fill_id="F1", side=FillSide.BUY, quantity=10, price="70000",
                       filled_at=datetime(2026, 5, 6, 9, 0, 0, tzinfo=timezone.utc)),
        ]
        affected = ledger.apply_fills(fills)
        # 정렬되어 적용 — 첫 매수(70000) → 둘째 매수(72000)
        # 가중 평균 = (10*70000 + 10*72000)/20 = 71000
        assert affected["005930"].avg_cost_krw == Decimal("71000")
        assert affected["005930"].quantity == 20
        print(f"   ✅ 자동 정렬 후 가중평균 = 71000")
    finally:
        Path(db_path).unlink()


def test_ledger_full_buy_sell_cycle():
    print("\n[13] 매수-매도 전체 사이클 (시나리오)")
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db_path = f.name
    try:
        ledger = PositionLedger(PositionStore(db_path))
        
        # 1) BUY 10@70000, fee 100
        ledger.apply_fill(_make_fill(
            fill_id="F1", side=FillSide.BUY, quantity=10, price="70000",
            fee_krw="100",
            filled_at=datetime(2026, 5, 6, 9, 0, 0, tzinfo=timezone.utc),
        ))
        
        # 2) BUY 10@72000, fee 100
        ledger.apply_fill(_make_fill(
            fill_id="F2", side=FillSide.BUY, quantity=10, price="72000",
            fee_krw="100",
            filled_at=datetime(2026, 5, 6, 10, 0, 0, tzinfo=timezone.utc),
        ))
        
        # 평균 = (10*70000 + 100 + 10*72000 + 100) / 20 = 1420200/20 = 71010
        s = ledger.get("005930")
        assert s.quantity == 20
        assert s.avg_cost_krw == Decimal("71010")
        assert s.realized_pnl_krw == Decimal("0")
        
        # 3) SELL 5@75000, fee 50, tax 600
        ledger.apply_fill(_make_fill(
            fill_id="F3", side=FillSide.SELL, quantity=5, price="75000",
            fee_krw="50", tax_krw="600",
            filled_at=datetime(2026, 5, 6, 11, 0, 0, tzinfo=timezone.utc),
        ))
        # gross = 5*75000 = 375000
        # cost_basis = 5*71010 = 355050
        # realized = 375000 - 355050 - 50 - 600 = 19300
        s2 = ledger.get("005930")
        assert s2.quantity == 15
        assert s2.realized_pnl_krw == Decimal("19300")
        assert s2.avg_cost_krw == Decimal("71010")  # 변화 없음
        
        # 4) SELL 15@76000 (전량매도), fee 100, tax 1900
        ledger.apply_fill(_make_fill(
            fill_id="F4", side=FillSide.SELL, quantity=15, price="76000",
            fee_krw="100", tax_krw="1900",
            filled_at=datetime(2026, 5, 6, 12, 0, 0, tzinfo=timezone.utc),
        ))
        # gross = 15*76000 = 1140000
        # cost_basis = 15*71010 = 1065150
        # realized_delta = 1140000 - 1065150 - 100 - 1900 = 72850
        # 누적 realized = 19300 + 72850 = 92150
        s3 = ledger.get("005930")
        assert s3.quantity == 0
        assert s3.avg_cost_krw == Decimal("0")  # reset!
        assert s3.realized_pnl_krw == Decimal("92150")
        print(f"   ✅ 4건 처리, 최종: qty=0, realized={s3.realized_pnl_krw}, fees={s3.total_fees_krw}, taxes={s3.total_taxes_krw}")
    finally:
        Path(db_path).unlink()


def test_ledger_history():
    print("\n[14] 변경 이력 조회")
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db_path = f.name
    try:
        ledger = PositionLedger(PositionStore(db_path))
        ledger.apply_fill(_make_fill(
            fill_id="F1", side=FillSide.BUY, quantity=10, price="70000",
            filled_at=datetime(2026, 5, 6, 9, 0, 0, tzinfo=timezone.utc),
        ))
        ledger.apply_fill(_make_fill(
            fill_id="F2", side=FillSide.SELL, quantity=5, price="75000",
            tax_krw="500",
            filled_at=datetime(2026, 5, 6, 10, 0, 0, tzinfo=timezone.utc),
        ))
        
        hist = ledger.history("005930")
        assert len(hist) == 2
        assert hist[0]["fill_id"] == "F1"
        assert hist[0]["realized_pnl_delta_krw"] == "0"
        assert hist[1]["fill_id"] == "F2"
        # delta = 5*(75000-70000) - 0 - 500 = 24500
        assert hist[1]["realized_pnl_delta_krw"] == "24500"
        print(f"   ✅ 이력 2건: F1(delta=0), F2(delta=24500)")
    finally:
        Path(db_path).unlink()


def test_ledger_rebuild_from_fills():
    print("\n[15] rebuild_from_fills (정합성 복구)")
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as fp_db:
        pos_db = fp_db.name
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as ff_db:
        fill_db = ff_db.name
    try:
        # FillStore에 fill 5개 저장
        fill_store = FillStore(fill_db)
        fills = [
            _make_fill(fill_id="F1", side=FillSide.BUY, quantity=10, price="70000",
                       filled_at=datetime(2026, 5, 6, 9, tzinfo=timezone.utc)),
            _make_fill(fill_id="F2", side=FillSide.BUY, quantity=10, price="72000",
                       filled_at=datetime(2026, 5, 6, 10, tzinfo=timezone.utc)),
            _make_fill(fill_id="F3", side=FillSide.SELL, quantity=5, price="75000",
                       tax_krw="500",
                       filled_at=datetime(2026, 5, 6, 11, tzinfo=timezone.utc)),
            _make_fill(fill_id="F4", side=FillSide.BUY, quantity=10, price="80000",
                       symbol="000660",
                       filled_at=datetime(2026, 5, 6, 12, tzinfo=timezone.utc)),
            _make_fill(fill_id="F5", side=FillSide.SELL, quantity=5, price="82000",
                       symbol="000660", tax_krw="800",
                       filled_at=datetime(2026, 5, 6, 13, tzinfo=timezone.utc)),
        ]
        fill_store.upsert_many(fills)
        
        # Ledger 재구축
        ledger = PositionLedger(PositionStore(pos_db))
        # 먼저 잘못된 상태 주입
        ledger._store.upsert(PositionState(
            symbol="005930", quantity=999,
            avg_cost_krw=Decimal("99999"),
            realized_pnl_krw=Decimal("0"),
            total_fees_krw=Decimal("0"), total_taxes_krw=Decimal("0"),
        ))
        
        # rebuild
        affected = ledger.rebuild_from_fills(fill_store)
        
        # 005930: 매수 20 → 매도 5 → 보유 15
        s = ledger.get("005930")
        assert s.quantity == 15
        # 000660: 매수 10 → 매도 5 → 보유 5
        s2 = ledger.get("000660")
        assert s2.quantity == 5
        print(f"   ✅ rebuild: 005930 qty=15, 000660 qty=5")
    finally:
        Path(pos_db).unlink()
        Path(fill_db).unlink()


def test_ledger_total_realized_pnl():
    print("\n[16] 누적 실현 P&L 합계")
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db_path = f.name
    try:
        ledger = PositionLedger(PositionStore(db_path))
        
        # 005930: 매수 후 일부 매도 → realized P&L
        ledger.apply_fill(_make_fill(
            fill_id="F1", side=FillSide.BUY, quantity=10, price="70000",
            filled_at=datetime(2026, 5, 6, 9, tzinfo=timezone.utc),
        ))
        ledger.apply_fill(_make_fill(
            fill_id="F2", side=FillSide.SELL, quantity=5, price="75000",
            tax_krw="500",
            filled_at=datetime(2026, 5, 6, 10, tzinfo=timezone.utc),
        ))
        
        # 000660: 매수 후 매도 → 다른 종목 실현 P&L
        ledger.apply_fill(_make_fill(
            fill_id="F3", side=FillSide.BUY, quantity=10, price="80000",
            symbol="000660",
            filled_at=datetime(2026, 5, 6, 11, tzinfo=timezone.utc),
        ))
        ledger.apply_fill(_make_fill(
            fill_id="F4", side=FillSide.SELL, quantity=10, price="82000",
            symbol="000660", tax_krw="1500",
            filled_at=datetime(2026, 5, 6, 12, tzinfo=timezone.utc),
        ))
        
        # F2 realized: 5*(75000-70000) - 500 = 24500
        # F4 realized: 10*(82000-80000) - 1500 = 18500
        # 합계 = 43000
        total = ledger.total_realized_pnl_krw()
        assert Decimal(total) == Decimal("43000")
        print(f"   ✅ 총 누적 realized P&L = {total}")
    finally:
        Path(db_path).unlink()


if __name__ == "__main__":
    test_empty_position()
    test_position_validation()
    test_buy_to_empty()
    test_buy_average_cost_weighted()
    test_sell_partial()
    test_sell_full_resets_avg()
    test_oversell_rejected()
    test_symbol_mismatch()
    test_store_roundtrip()
    test_store_get_all_active_only()
    test_ledger_apply_single()
    test_ledger_apply_fills_sorted()
    test_ledger_full_buy_sell_cycle()
    test_ledger_history()
    test_ledger_rebuild_from_fills()
    test_ledger_total_realized_pnl()
    print("\n🎉 모든 스모크 테스트 통과 (All smoke tests passed)")
