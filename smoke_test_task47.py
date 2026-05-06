"""스모크 테스트 (Smoke Test) — Task 47 v0.1 Portfolio Risk Controls."""

import csv
import sys
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.data.symbol_master import Symbol, SymbolMaster
from src.risk.gates import (
    GateResult, RiskContext, RiskGate,
    PortfolioExposureGate, SectorConcentrationGate, SingleOrderSizeGate,
)
from src.risk.portfolio_risk import (
    PortfolioRiskAnalyzer, PortfolioRiskConfig, PortfolioRiskSnapshot,
)


CSV_PATH = Path(__file__).parent / "data" / "reference" / "symbol_master.csv"


def _make_ctx(
    *, symbol="005930", side="buy", quantity=10, price="70000",
    equity_krw="10000000", available_cash_krw="10000000",
    open_positions=None, daily_realized_pnl_krw="0",
):
    p = Decimal(price)
    return RiskContext(
        symbol=symbol, side=side, quantity=quantity, price=p,
        estimated_cost_krw=p * Decimal(quantity),
        strategy_id="momentum_v04", intent_id=f"intent-{symbol}",
        instrument_type="stock",
        equity_krw=Decimal(equity_krw),
        available_cash_krw=Decimal(available_cash_krw),
        daily_realized_pnl_krw=Decimal(daily_realized_pnl_krw),
        open_positions=open_positions or {},
        pending_orders=[],
        market_now_utc=datetime.now(timezone.utc),
        market_is_open=True,
        last_quote_price=p,
        last_order_at_utc=None,
        last_order_for_symbol_utc=None,
    )


# ─────────────────────────────────────────────────
# Symbol Master 확장 검증
# ─────────────────────────────────────────────────

def test_symbol_master_sector_loaded():
    print("\n[1] Symbol Master sector 로드")
    sm = SymbolMaster.from_csv(CSV_PATH)
    # 핵심 종목 sector 확인
    assert sm.get("005930").sector == "tech"
    assert sm.get("005380").sector == "industrial"
    assert sm.get("051910").sector == "materials"
    assert sm.get("091990").sector == "healthcare"
    assert sm.get("069500").sector == "etf"
    assert sm.get("069500").is_etf() is True
    assert sm.get("005930").is_etf() is False
    print(f"   ✅ 10종목 sector 분류 정상")


def test_symbol_master_filter_by_sector():
    print("\n[2] filter_by_sector + all_sectors")
    sm = SymbolMaster.from_csv(CSV_PATH)
    tech = sm.filter_by_sector("tech")
    assert len(tech) == 4  # 005930, 000660, 035720, 035420
    assert "etf" in sm.all_sectors()
    assert "tech" in sm.all_sectors()
    print(f"   ✅ tech={len(tech)}개, sectors={sorted(sm.all_sectors())}")


def test_symbol_master_backward_compat_missing_sector():
    print("\n[3] 하위 호환 — sector 컬럼 누락 시 'unknown'")
    # 임시 CSV — sector 컬럼 없음
    tmp = tempfile.NamedTemporaryFile(suffix=".csv", delete=False, mode="w", encoding="utf-8")
    tmp.write("code,name_kr,name_en,market,instrument_type,lot_size,tick_policy,status,currency,listed_date,updated_at\n")
    tmp.write("999999,테스트,Test,KOSPI,stock,1,krx_stock,active,KRW,2026-01-01,2026-05-06T00:00:00+00:00\n")
    tmp.close()
    try:
        sm = SymbolMaster.from_csv(tmp.name)
        assert sm.get("999999").sector == "unknown"
        print(f"   ✅ sector 컬럼 누락 → 'unknown' 자동 적용")
    finally:
        Path(tmp.name).unlink()


def test_get_sector_unknown_symbol():
    print("\n[4] get_sector — 미상장 종목 default 반환")
    sm = SymbolMaster.from_csv(CSV_PATH)
    assert sm.get_sector("999999") == "unknown"
    assert sm.get_sector("999999", default="other") == "other"
    print(f"   ✅ 미상장 → default 'unknown'")


# ─────────────────────────────────────────────────
# PortfolioExposureGate
# ─────────────────────────────────────────────────

def test_portfolio_exposure_pass():
    print("\n[5] PortfolioExposureGate — 한도 내 통과")
    gate = PortfolioExposureGate(max_total_exposure_pct=Decimal("0.80"))
    # 자본 1천만, 현재 노출 5백만, 추가 200만 매수 → 70% < 80%
    ctx = _make_ctx(
        equity_krw="10000000",
        open_positions={"005930": {"market_value_krw": "5000000"}},
        quantity=20, price="100000",  # 200만 매수
    )
    result = gate.evaluate(ctx)
    assert result.outcome == "pass"
    print(f"   ✅ 70% < 80% → pass")


def test_portfolio_exposure_reject():
    print("\n[6] PortfolioExposureGate — 한도 초과 거부")
    gate = PortfolioExposureGate(max_total_exposure_pct=Decimal("0.80"))
    # 자본 1천만, 현재 7백만, 200만 추가 → 90% > 80%
    ctx = _make_ctx(
        equity_krw="10000000",
        open_positions={"005930": {"market_value_krw": "7000000"}},
        quantity=20, price="100000",
    )
    result = gate.evaluate(ctx)
    assert result.outcome == "reject"
    assert "전체 노출" in result.reason or "exceeded" in result.reason
    print(f"   ✅ 90% > 80% → reject")


def test_portfolio_exposure_sell_passes():
    print("\n[7] PortfolioExposureGate — SELL은 항상 pass")
    gate = PortfolioExposureGate(max_total_exposure_pct=Decimal("0.10"))
    ctx = _make_ctx(side="sell", equity_krw="10000000",
                    open_positions={"005930": {"market_value_krw": "9000000"}})
    assert gate.evaluate(ctx).outcome == "pass"
    print(f"   ✅ SELL → pass")


def test_portfolio_exposure_zero_equity_rejects():
    print("\n[8] PortfolioExposureGate — equity=0 fail-closed")
    gate = PortfolioExposureGate(max_total_exposure_pct=Decimal("0.80"))
    ctx = _make_ctx(equity_krw="0", available_cash_krw="0")
    result = gate.evaluate(ctx)
    assert result.outcome == "reject"
    print(f"   ✅ equity=0 → reject (fail-closed)")


def test_portfolio_exposure_aggregates_multiple_symbols():
    print("\n[9] PortfolioExposureGate — 다종목 합산")
    gate = PortfolioExposureGate(max_total_exposure_pct=Decimal("0.80"))
    ctx = _make_ctx(
        equity_krw="10000000",
        open_positions={
            "005930": {"market_value_krw": "3000000"},
            "000660": {"market_value_krw": "3000000"},
            "035720": {"market_value_krw": "1000000"},
        },
        quantity=10, price="100000",  # 100만 추가 → 800만 / 1천만 = 80%
    )
    # 정확히 80% — 한도 내
    result = gate.evaluate(ctx)
    assert result.outcome == "pass"
    print(f"   ✅ 다종목 합산 700만 + 100만 = 80% → pass")


# ─────────────────────────────────────────────────
# SectorConcentrationGate
# ─────────────────────────────────────────────────

def test_sector_concentration_pass():
    print("\n[10] SectorConcentrationGate — 한도 내 통과")
    sm = SymbolMaster.from_csv(CSV_PATH)
    gate = SectorConcentrationGate(sm, max_sector_exposure_pct=Decimal("0.40"))
    # 005930 (tech) 매수, 현재 tech 200만 — 자본 1천만 → 200만+200만=400만=40%
    ctx = _make_ctx(
        symbol="005930",
        equity_krw="10000000",
        open_positions={"000660": {"market_value_krw": "2000000"}},  # tech
        quantity=20, price="100000",  # 200만 매수
    )
    result = gate.evaluate(ctx)
    assert result.outcome == "pass"
    print(f"   ✅ tech 40% = 한도 → pass")


def test_sector_concentration_reject():
    print("\n[11] SectorConcentrationGate — 같은 섹터 한도 초과")
    sm = SymbolMaster.from_csv(CSV_PATH)
    gate = SectorConcentrationGate(sm, max_sector_exposure_pct=Decimal("0.40"))
    # tech 이미 380만 + 30만 추가 → 41% > 40%
    ctx = _make_ctx(
        symbol="005930",
        equity_krw="10000000",
        open_positions={
            "000660": {"market_value_krw": "2000000"},
            "035420": {"market_value_krw": "1800000"},
        },
        quantity=3, price="100000",  # 30만 매수
    )
    result = gate.evaluate(ctx)
    assert result.outcome == "reject"
    assert "tech" in result.reason
    assert "섹터" in result.reason or "sector" in result.reason
    print(f"   ✅ tech 41% > 40% → reject (sector=tech)")


def test_sector_concentration_different_sector_passes():
    print("\n[12] SectorConcentrationGate — 다른 섹터로 분산 → pass")
    sm = SymbolMaster.from_csv(CSV_PATH)
    gate = SectorConcentrationGate(sm, max_sector_exposure_pct=Decimal("0.40"))
    # tech에 380만 보유, healthcare 매수 → healthcare 노출은 새로 발생 (200만)
    ctx = _make_ctx(
        symbol="091990",  # healthcare
        equity_krw="10000000",
        open_positions={
            "000660": {"market_value_krw": "2000000"},
            "035420": {"market_value_krw": "1800000"},
        },
        quantity=20, price="100000",  # 200만 매수
    )
    result = gate.evaluate(ctx)
    assert result.outcome == "pass"
    print(f"   ✅ healthcare 분산 → pass (tech 보유와 무관)")


def test_sector_concentration_etf_exempted():
    print("\n[13] SectorConcentrationGate — ETF 면제")
    sm = SymbolMaster.from_csv(CSV_PATH)
    gate = SectorConcentrationGate(
        sm, max_sector_exposure_pct=Decimal("0.10"),  # 매우 낮은 한도
        exempt_etf=True,
    )
    # KODEX 200 매수 — 자본의 50% → 한도 10% 초과지만 ETF 면제
    ctx = _make_ctx(
        symbol="069500",
        equity_krw="10000000",
        quantity=50, price="100000",  # 500만 매수
    )
    result = gate.evaluate(ctx)
    assert result.outcome == "pass"
    assert "etf" in str(result.detail).lower()
    print(f"   ✅ ETF는 sector 검사 면제")


def test_sector_concentration_etf_not_exempted():
    print("\n[14] SectorConcentrationGate — exempt_etf=False")
    sm = SymbolMaster.from_csv(CSV_PATH)
    gate = SectorConcentrationGate(
        sm, max_sector_exposure_pct=Decimal("0.10"),
        exempt_etf=False,  # ETF도 검사
    )
    # KODEX 200 — 50% > 10% → reject
    ctx = _make_ctx(
        symbol="069500",
        equity_krw="10000000",
        quantity=50, price="100000",
    )
    result = gate.evaluate(ctx)
    assert result.outcome == "reject"
    print(f"   ✅ exempt_etf=False → ETF도 검사 → reject")


def test_sector_concentration_unknown_symbol_passes():
    print("\n[15] SectorConcentrationGate — 미상장 종목 pass (다른 게이트가 처리)")
    sm = SymbolMaster.from_csv(CSV_PATH)
    gate = SectorConcentrationGate(sm, max_sector_exposure_pct=Decimal("0.40"))
    ctx = _make_ctx(symbol="999999", equity_krw="10000000")
    result = gate.evaluate(ctx)
    assert result.outcome == "pass"
    assert "unknown" in result.reason.lower() or "skip" in result.reason.lower()
    print(f"   ✅ 미상장 → pass (다른 게이트 위임)")


def test_sector_concentration_sell_passes():
    print("\n[16] SectorConcentrationGate — SELL pass")
    sm = SymbolMaster.from_csv(CSV_PATH)
    gate = SectorConcentrationGate(sm, max_sector_exposure_pct=Decimal("0.10"))
    ctx = _make_ctx(side="sell", equity_krw="10000000")
    assert gate.evaluate(ctx).outcome == "pass"
    print(f"   ✅ SELL → pass")


# ─────────────────────────────────────────────────
# SingleOrderSizeGate
# ─────────────────────────────────────────────────

def test_single_order_size_pass():
    print("\n[17] SingleOrderSizeGate — 한도 내")
    gate = SingleOrderSizeGate(max_single_order_pct_of_equity=Decimal("0.10"))
    # 자본 1천만, 100만 주문 = 10%
    ctx = _make_ctx(equity_krw="10000000", quantity=10, price="100000")
    result = gate.evaluate(ctx)
    assert result.outcome == "pass"
    print(f"   ✅ 10% = 한도 → pass")


def test_single_order_size_reject():
    print("\n[18] SingleOrderSizeGate — 한도 초과")
    gate = SingleOrderSizeGate(max_single_order_pct_of_equity=Decimal("0.10"))
    # 자본 1천만, 200만 주문 = 20% > 10%
    ctx = _make_ctx(equity_krw="10000000", quantity=20, price="100000")
    result = gate.evaluate(ctx)
    assert result.outcome == "reject"
    assert "단일 주문" in result.reason or "single order" in result.reason.lower()
    print(f"   ✅ 20% > 10% → reject")


def test_single_order_size_sell_passes():
    print("\n[19] SingleOrderSizeGate — SELL pass")
    gate = SingleOrderSizeGate(max_single_order_pct_of_equity=Decimal("0.01"))
    ctx = _make_ctx(side="sell", equity_krw="10000000",
                    quantity=100, price="100000")  # 1천만
    assert gate.evaluate(ctx).outcome == "pass"
    print(f"   ✅ SELL → pass")


# ─────────────────────────────────────────────────
# PortfolioRiskAnalyzer
# ─────────────────────────────────────────────────

def test_analyzer_basic():
    print("\n[20] PortfolioRiskAnalyzer.analyze — 기본")
    sm = SymbolMaster.from_csv(CSV_PATH)
    analyzer = PortfolioRiskAnalyzer(sm)
    snapshot = analyzer.analyze(
        positions={
            "005930": {"market_value_krw": "3000000"},  # tech
            "000660": {"market_value_krw": "1000000"},  # tech
            "005380": {"market_value_krw": "1500000"},  # industrial
            "069500": {"market_value_krw": "2000000"},  # etf
        },
        equity_krw=Decimal("10000000"),
    )
    # total = 3+1+1.5+2 = 7.5M / 10M = 75%
    assert snapshot.total_exposure_krw == Decimal("7500000")
    assert snapshot.total_exposure_pct == Decimal("0.75")
    # tech = 4M = 40%
    assert snapshot.by_sector_exposure_krw["tech"] == Decimal("4000000")
    assert snapshot.by_sector_exposure_pct["tech"] == Decimal("0.4")
    # industrial = 1.5M = 15%
    assert snapshot.by_sector_exposure_krw["industrial"] == Decimal("1500000")
    # etf = 2M (별도 bucket)
    assert snapshot.by_sector_exposure_krw["etf"] == Decimal("2000000")
    # max_sector (non-etf) = tech
    assert snapshot.max_sector == "tech"
    assert snapshot.position_count == 4
    assert snapshot.etf_count == 1
    assert snapshot.non_etf_count == 3
    print(f"   ✅ total=75%, tech=40%, industrial=15%, etf=20%")


def test_analyzer_warnings_total_exposure():
    print("\n[21] Analyzer — 전체 노출 한도 초과 경고")
    sm = SymbolMaster.from_csv(CSV_PATH)
    config = PortfolioRiskConfig(
        max_total_exposure_pct=Decimal("0.50"),
    )
    analyzer = PortfolioRiskAnalyzer(sm, config)
    snapshot = analyzer.analyze(
        positions={"005930": {"market_value_krw": "8000000"}},
        equity_krw=Decimal("10000000"),
    )
    # 80% > 50% → 경고
    assert snapshot.has_warnings()
    assert any("전체 노출" in w or "total" in w.lower() for w in snapshot.warnings)
    print(f"   ✅ 경고 {len(snapshot.warnings)}개: {snapshot.warnings[0][:50]}...")


def test_analyzer_warnings_sector_concentration():
    print("\n[22] Analyzer — 섹터 집중 경고")
    sm = SymbolMaster.from_csv(CSV_PATH)
    config = PortfolioRiskConfig(
        max_sector_exposure_pct=Decimal("0.30"),
        max_total_exposure_pct=Decimal("0.99"),  # 다른 경고 회피
    )
    analyzer = PortfolioRiskAnalyzer(sm, config)
    snapshot = analyzer.analyze(
        positions={
            "005930": {"market_value_krw": "2500000"},
            "000660": {"market_value_krw": "2000000"},  # tech 합 4.5M = 45% > 30%
        },
        equity_krw=Decimal("10000000"),
    )
    assert any("섹터" in w or "sector" in w.lower() for w in snapshot.warnings)
    print(f"   ✅ tech 45% > 30% → 섹터 경고")


def test_analyzer_warnings_diversification():
    print("\n[23] Analyzer — 분산 부족 경고")
    sm = SymbolMaster.from_csv(CSV_PATH)
    config = PortfolioRiskConfig(
        sector_min_diversification=2,
        max_total_exposure_pct=Decimal("0.99"),
        max_sector_exposure_pct=Decimal("0.99"),
    )
    analyzer = PortfolioRiskAnalyzer(sm, config)
    snapshot = analyzer.analyze(
        positions={"005930": {"market_value_krw": "1000000"}},  # tech 1개만
        equity_krw=Decimal("10000000"),
    )
    assert any("분산" in w or "diversif" in w.lower() for w in snapshot.warnings)
    print(f"   ✅ 활성 섹터 1개 < 최소 2개 → 분산 경고")


def test_analyzer_project_buy():
    print("\n[24] Analyzer.project — BUY 예상 상태")
    sm = SymbolMaster.from_csv(CSV_PATH)
    analyzer = PortfolioRiskAnalyzer(sm)
    # 현재 tech 0%, project 005930 BUY 100만 → tech 10%
    snapshot = analyzer.project(
        positions={},
        equity_krw=Decimal("10000000"),
        candidate_symbol="005930",
        candidate_side="buy",
        candidate_quantity=10,
        candidate_price=Decimal("100000"),  # 100만
    )
    assert snapshot.total_exposure_krw == Decimal("1000000")
    assert snapshot.by_sector_exposure_pct["tech"] == Decimal("0.1")
    print(f"   ✅ project BUY 005930 100만 → tech=10%")


def test_analyzer_project_sell():
    print("\n[25] Analyzer.project — SELL 예상 상태")
    sm = SymbolMaster.from_csv(CSV_PATH)
    analyzer = PortfolioRiskAnalyzer(sm)
    snapshot = analyzer.project(
        positions={"005930": {"market_value_krw": "5000000"}},
        equity_krw=Decimal("10000000"),
        candidate_symbol="005930",
        candidate_side="sell",
        candidate_quantity=10,
        candidate_price=Decimal("200000"),  # 200만 매도
    )
    # 5M - 2M = 3M 잔여
    assert snapshot.total_exposure_krw == Decimal("3000000")
    print(f"   ✅ project SELL 200만 → 잔여 300만")


def test_analyzer_to_dict_serializable():
    print("\n[26] PortfolioRiskSnapshot.to_dict — JSON 직렬화")
    import json
    sm = SymbolMaster.from_csv(CSV_PATH)
    analyzer = PortfolioRiskAnalyzer(sm)
    snapshot = analyzer.analyze(
        positions={"005930": {"market_value_krw": "1000000"}},
        equity_krw=Decimal("10000000"),
    )
    d = snapshot.to_dict()
    json_str = json.dumps(d, ensure_ascii=False)
    assert "captured_at_utc" in d
    assert "by_sector_exposure_krw" in d
    assert isinstance(d["total_exposure_krw"], str)  # Decimal → str
    print(f"   ✅ JSON 직렬화 OK")


# ─────────────────────────────────────────────────
# 입력 검증
# ─────────────────────────────────────────────────

def test_invalid_inputs():
    print("\n[27] 잘못된 입력 거부")
    # PortfolioExposureGate: 한도 0
    try:
        PortfolioExposureGate(max_total_exposure_pct=Decimal("0"))
        assert False
    except ValueError:
        print(f"   ✅ PortfolioExposureGate 한도 0 거부")

    # SingleOrderSizeGate: 한도 > 1
    try:
        SingleOrderSizeGate(max_single_order_pct_of_equity=Decimal("1.5"))
        assert False
    except ValueError:
        print(f"   ✅ SingleOrderSizeGate 한도 > 1 거부")

    # PortfolioRiskConfig 검증
    try:
        PortfolioRiskConfig(max_total_exposure_pct=Decimal("-0.1"))
        assert False
    except ValueError:
        print(f"   ✅ PortfolioRiskConfig 음수 거부")

    # Analyzer.analyze: tz-naive
    sm = SymbolMaster.from_csv(CSV_PATH)
    analyzer = PortfolioRiskAnalyzer(sm)
    try:
        analyzer.analyze(positions={}, equity_krw=Decimal("0"),
                         as_of_utc=datetime.now())
        assert False
    except ValueError:
        print(f"   ✅ tz-naive 거부")


def test_combined_workflow():
    print("\n[28] 통합 워크플로우 — 3개 게이트 + Analyzer")
    sm = SymbolMaster.from_csv(CSV_PATH)
    config = PortfolioRiskConfig(
        max_total_exposure_pct=Decimal("0.80"),
        max_sector_exposure_pct=Decimal("0.40"),
        max_single_order_pct_of_equity=Decimal("0.10"),
        sector_min_diversification=1,  # 시나리오는 단일 섹터(tech) 사용
    )

    # 게이트 체인
    gates = [
        SingleOrderSizeGate(config.max_single_order_pct_of_equity),
        PortfolioExposureGate(config.max_total_exposure_pct),
        SectorConcentrationGate(sm, config.max_sector_exposure_pct, exempt_etf=True),
    ]
    analyzer = PortfolioRiskAnalyzer(sm, config)

    # 시나리오: tech 35% 보유, 005930 추가 매수 5% → 총 tech 40%, 한도 내
    ctx = _make_ctx(
        symbol="005930",  # tech
        equity_krw="10000000",
        open_positions={
            "000660": {"market_value_krw": "2000000"},
            "035720": {"market_value_krw": "1500000"},
        },
        quantity=5, price="100000",  # 50만 매수 (5%)
    )

    results = [g.evaluate(ctx) for g in gates]
    for r in results:
        assert r.outcome == "pass", f"{r.gate_name} failed: {r.reason}"

    # 사후 분석
    proj = analyzer.project(
        positions=ctx.open_positions,
        equity_krw=ctx.equity_krw,
        candidate_symbol=ctx.symbol,
        candidate_side=ctx.side,
        candidate_quantity=ctx.quantity,
        candidate_price=ctx.price,
    )
    assert proj.by_sector_exposure_pct["tech"] == Decimal("0.4")  # 40%
    assert not proj.has_warnings(), f"unexpected warnings: {proj.warnings}"
    print(f"   ✅ 3 gates pass + projected tech 40% (한도 내)")


if __name__ == "__main__":
    test_symbol_master_sector_loaded()
    test_symbol_master_filter_by_sector()
    test_symbol_master_backward_compat_missing_sector()
    test_get_sector_unknown_symbol()
    test_portfolio_exposure_pass()
    test_portfolio_exposure_reject()
    test_portfolio_exposure_sell_passes()
    test_portfolio_exposure_zero_equity_rejects()
    test_portfolio_exposure_aggregates_multiple_symbols()
    test_sector_concentration_pass()
    test_sector_concentration_reject()
    test_sector_concentration_different_sector_passes()
    test_sector_concentration_etf_exempted()
    test_sector_concentration_etf_not_exempted()
    test_sector_concentration_unknown_symbol_passes()
    test_sector_concentration_sell_passes()
    test_single_order_size_pass()
    test_single_order_size_reject()
    test_single_order_size_sell_passes()
    test_analyzer_basic()
    test_analyzer_warnings_total_exposure()
    test_analyzer_warnings_sector_concentration()
    test_analyzer_warnings_diversification()
    test_analyzer_project_buy()
    test_analyzer_project_sell()
    test_analyzer_to_dict_serializable()
    test_invalid_inputs()
    test_combined_workflow()
    print("\n🎉 모든 스모크 테스트 통과 (All smoke tests passed)")
