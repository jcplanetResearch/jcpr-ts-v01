"""스모크 테스트 (Smoke Test) — Task 16 v0.3 SignalRunner."""

import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# Task 16 v0.3
from src.signals.runner import (
    CycleAborted, CycleResult, RunnerConfig, SignalRunner,
)
from src.signals.runner_audit import CycleAuditLog, build_cycle_record

# 의존
from src.brokers.kis import KISAdapter, KISCredentials, KISEnv
from src.data.dummy_quote_source import DummyQuoteSource
from src.data.dummy_source import DummySource
from src.data.ohlcv_schema import Timeframe
from src.data.ohlcv_store import OHLCVStore
from src.data.quote_store import QuoteStore
from src.data.symbol_master import SymbolMaster
from src.execution.execution_record import ExecutionAuditLog, ExecutionOutcome
from src.execution.gateway import ExecutionGateway
from src.execution.shutdown_check import ShutdownChecker
from src.execution.sizing import CapacityConfig, OrderSizer
from src.execution.sizing_audit import SizingAuditLogger
from src.risk.gates import (
    DailyLossLimitGate, DuplicateOrderGate, ExposureGate,
    KillSwitchGate, MarketStateGate, OrderRateLimitGate,
    PositionLimitGate, PriceSanityGate,
)
from src.risk.risk_audit import RiskAuditLogger
from src.risk.risk_gate import RiskGateRunner
from src.signals.strategies.momentum_v04 import MomentumStrategyV04


CSV_PATH = Path(__file__).parent / "data" / "reference" / "symbol_master.csv"


# ─────────────────────────────────────────────────
# Stub HTTP Session (Task 8/21 패턴 재사용)
# ─────────────────────────────────────────────────

class StubResponse:
    def __init__(self, status_code, json_data):
        self.status_code = status_code
        self._json = json_data
    def json(self):
        return self._json


class StubSession:
    def __init__(self):
        self._token_count = 0

    def post(self, url, data=None, headers=None, timeout=None):
        if "/oauth2/tokenP" in url:
            self._token_count += 1
            return StubResponse(200, {
                "access_token": f"stub_{self._token_count}",
                "token_type": "Bearer", "expires_in": 86400,
            })
        if "order-cash" in url:
            return StubResponse(200, {
                "rt_cd": "0", "msg_cd": "OK", "msg1": "주문 성공",
                "output": {"ODNO": "0009999111"},
            })
        return StubResponse(404, {"rt_cd": "1"})

    def get(self, url, headers=None, params=None, timeout=None):
        if "inquire-balance" in url:
            return StubResponse(200, {
                "rt_cd": "0", "msg_cd": "OK", "msg1": "",
                "output1": [],
                "output2": [{
                    "dnca_tot_amt": "10000000", "ord_psbl_cash": "9500000",
                    "tot_evlu_amt": "10000000",
                    "pchs_amt_smtl_amt": "0", "evlu_pfls_smtl_amt": "0",
                }],
            })
        if "inquire-asking-price" in url:
            return StubResponse(200, {
                "rt_cd": "0", "msg_cd": "OK", "msg1": "",
                "output1": {
                    "stck_prpr": "70500",
                    "aspr_acpt_hour": datetime.now(timezone.utc).strftime("%H%M%S"),
                    **{f"askp{i}": str(70500 + i*100) for i in range(1, 11)},
                    **{f"askp_rsqn{i}": str(1000 - i*50) for i in range(1, 11)},
                    **{f"bidp{i}": str(70400 - (i-1)*100) for i in range(1, 11)},
                    **{f"bidp_rsqn{i}": str(900 - i*40) for i in range(1, 11)},
                },
                "output2": [],
            })
        if "inquire-psbl-rvsecncl" in url:
            return StubResponse(200, {"rt_cd": "0", "msg_cd": "OK", "msg1": "", "output": []})
        return StubResponse(404, {"rt_cd": "1"})


# ─────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────

def _build_runner(*, watchlist=("005930",), min_interval=5, mode="explicit",
                  shutdown_event=None, kill_switch_path=None,
                  market_open_provider=None, audit_dir=None):
    """완전한 컴포넌트 통합 SignalRunner."""
    audit_dir = audit_dir or Path(tempfile.mkdtemp(prefix="jcpr_audit_"))

    # 데이터 채우기 — DummySource로 OHLCV 생성
    ohlcv_db = audit_dir / "ohlcv.sqlite"
    ohlcv_store = OHLCVStore(ohlcv_db)
    src = DummySource()
    bars = list(src.fetch_bars(
        watchlist[0] if watchlist else "005930", Timeframe.D1,
        datetime(2026, 4, 1, tzinfo=timezone.utc),
        datetime(2026, 5, 5, tzinfo=timezone.utc),
    ))
    ohlcv_store.upsert_bars(bars)
    # 다른 종목들도 채움
    for sym in watchlist[1:]:
        more_bars = list(src.fetch_bars(
            sym, Timeframe.D1,
            datetime(2026, 4, 1, tzinfo=timezone.utc),
            datetime(2026, 5, 5, tzinfo=timezone.utc),
        ))
        ohlcv_store.upsert_bars(more_bars)

    # Quote
    quote_db = audit_dir / "quote.sqlite"
    quote_store = QuoteStore(quote_db)
    qsrc = DummyQuoteSource()
    for sym in watchlist:
        snap = qsrc.snapshot(sym, fixed_time=datetime.now(timezone.utc))
        quote_store.upsert(snap)

    # KIS Adapter
    creds = KISCredentials(
        env=KISEnv.PAPER, app_key="x"*20, app_secret="y"*20,
        account_no="12345678-01",
    )
    adapter = KISAdapter(creds, http_session=StubSession())

    # Strategy
    sm = SymbolMaster.from_csv(CSV_PATH)
    strategy = MomentumStrategyV04(
        ohlcv_store=ohlcv_store, quote_store=quote_store, symbol_master=sm,
    )

    # Sizer
    sizer = OrderSizer(
        capacity=CapacityConfig(
            max_per_order_krw=Decimal("5000000"),
            max_per_day_krw=Decimal("20000000"),
            max_pct_of_equity=Decimal("0.20"),
            min_per_order_krw=Decimal("10000"),
        ),
        audit_logger=SizingAuditLogger(audit_dir / "sizing.jsonl"),
    )

    # Risk gates
    gates = [
        KillSwitchGate(kill_switch_path=kill_switch_path or "/tmp/__nonexistent__"),
        MarketStateGate(),
        OrderRateLimitGate(global_min_interval_sec=5, per_symbol_cooldown_sec=30),
        DuplicateOrderGate(),
        PriceSanityGate(max_deviation_pct=Decimal("0.05")),
        PositionLimitGate(max_positions=10),
        ExposureGate(max_pct_per_symbol=Decimal("0.20")),
        DailyLossLimitGate(max_daily_loss_krw=Decimal("500000")),
    ]
    risk_runner = RiskGateRunner(gates, RiskAuditLogger(audit_dir / "risk.jsonl"))

    # Gateway
    gateway = ExecutionGateway(
        adapter=adapter, symbol_master=sm, sizer=sizer,
        risk_runner=risk_runner,
        audit_log=ExecutionAuditLog(audit_dir / "executions.jsonl"),
    )

    # Shutdown
    shutdown = ShutdownChecker(
        kill_switch_path=kill_switch_path or "/tmp/__nonexistent__",
        shutdown_event=shutdown_event,
    )

    # Runner
    runner = SignalRunner(
        strategy=strategy, gateway=gateway, symbol_master=sm,
        shutdown=shutdown,
        cycle_audit=CycleAuditLog(audit_dir / "cycles.jsonl"),
        config=RunnerConfig(
            timeframe=Timeframe.D1,
            watchlist_mode=mode,
            explicit_watchlist=tuple(watchlist) if mode == "explicit" else (),
            min_symbol_interval_sec=min_interval,
            cycle_interval_sec=2,  # 테스트용 짧게
            shutdown_poll_interval_sec=0.05,
        ),
        market_is_open_provider=market_open_provider,
    )
    return runner, audit_dir


# ─────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────

def test_explicit_watchlist_required():
    print("\n[1] explicit 모드 + 빈 watchlist → ValueError")
    try:
        _build_runner(watchlist=(), mode="explicit")
        assert False
    except ValueError as e:
        assert "explicit_watchlist" in str(e) or "비어있음" in str(e)
        print(f"   ✅ 빈 explicit watchlist 거부: {e}")


def test_min_interval_validation():
    print("\n[2] min_symbol_interval_sec < 5 → ValueError")
    try:
        _build_runner(min_interval=3)
        assert False
    except ValueError as e:
        assert "min_symbol_interval_sec" in str(e)
        print(f"   ✅ < 5 거부")


def test_single_cycle_one_symbol():
    print("\n[3] 단일 사이클 — 1종목")
    runner, _ = _build_runner(watchlist=("005930",))
    result = runner.run_cycle()
    
    assert isinstance(result, CycleResult)
    assert not result.aborted
    assert result.stats.total_symbols == 1
    print(f"   ✅ stats={result.stats.as_dict()}")


def test_cycle_min_interval_enforced():
    print("\n[4] 종목간 최소 간격 ≥5초 강제")
    runner, _ = _build_runner(
        watchlist=("005930", "000660"),
        min_interval=5,
    )
    start = time.monotonic()
    result = runner.run_cycle()
    elapsed = time.monotonic() - start
    
    # 2종목, 첫 종목 후 5초 대기 → 최소 5초 이상
    # (실제 처리 시간 + 5초 wait)
    assert elapsed >= 5.0, f"5초 미만 경과: {elapsed:.2f}s"
    assert not result.aborted
    print(f"   ✅ 2종목 처리: {elapsed:.2f}s 경과 (≥5s 강제)")


def test_market_closed_skip():
    print("\n[5] 시장 미개장 → skip_when_market_closed")
    runner, _ = _build_runner(
        watchlist=("005930",),
        market_open_provider=lambda now: False,  # 항상 닫힘
    )
    result = runner.run_cycle()
    assert result.aborted is True
    assert result.abort_reason == "market_closed"
    print(f"   ✅ 시장 미개장 → cycle abort")


def test_kill_switch_at_cycle_start():
    print("\n[6] Kill switch active → 사이클 시작 즉시 중단")
    with tempfile.TemporaryDirectory() as td:
        kill_path = Path(td) / "KILL_SWITCH_ON"
        kill_path.write_text("active")
        
        runner, _ = _build_runner(
            watchlist=("005930",),
            kill_switch_path=str(kill_path),
        )
        result = runner.run_cycle()
        assert result.aborted is True
        assert "kill_switch" in result.abort_reason
        # 종목 처리도 안 됨
        assert len(result.executions) == 0
        print(f"   ✅ kill switch → cycle abort: {result.abort_reason}")


def test_shutdown_event_during_interval():
    print("\n[7] interval 대기 중 shutdown event → 즉시 응답 (≤500ms)")
    event = threading.Event()
    runner, _ = _build_runner(
        watchlist=("005930", "000660", "035720"),
        min_interval=10,  # 길게
        shutdown_event=event,
    )
    
    # 별도 스레드에서 0.5초 후 event set
    def set_event_after(delay):
        time.sleep(delay)
        event.set()
    
    t = threading.Thread(target=set_event_after, args=(0.5,))
    t.start()
    
    start = time.monotonic()
    result = runner.run_cycle()
    elapsed = time.monotonic() - start
    t.join()
    
    # 첫 종목은 처리되고, 두 번째 종목 전 wait 중에 abort
    assert result.aborted is True
    # 0.5초 set + ~0.1초 응답 마진 — 10초 wait를 다 기다리지 않음
    assert elapsed < 5.0, f"shutdown 응답 너무 느림: {elapsed:.2f}s"
    assert "shutdown" in result.abort_reason.lower()
    print(f"   ✅ interval wait 중 abort: elapsed={elapsed:.2f}s (≤5s)")


def test_audit_log_written():
    print("\n[8] 사이클 audit log 기록 확인")
    with tempfile.TemporaryDirectory() as td:
        audit_dir = Path(td)
        runner, _ = _build_runner(
            watchlist=("005930",),
            audit_dir=audit_dir,
        )
        runner.run_cycle()
        
        cycles_jsonl = audit_dir / "cycles.jsonl"
        assert cycles_jsonl.exists()
        
        import json
        with cycles_jsonl.open("r", encoding="utf-8") as f:
            lines = [l for l in f.readlines() if l.strip()]
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert "cycle_id" in rec
        assert "stats" in rec
        assert "watchlist" in rec
        assert "005930" in rec["watchlist"]
        # 비밀 노출 검사
        raw = "\n".join(lines)
        assert "x" * 20 not in raw
        assert "y" * 20 not in raw
        assert "12345678-01" not in raw
        print(f"   ✅ audit 기록: cycle_id={rec['cycle_id']}, 비밀 누출 없음")


def test_run_forever_max_cycles():
    print("\n[9] run_forever — max_cycles=2 후 종료")
    runner, _ = _build_runner(watchlist=("005930",))
    n = runner.run_forever(max_cycles=2)
    assert n == 2
    print(f"   ✅ {n} 사이클 실행 후 종료")


def test_run_forever_shutdown_during_wait():
    print("\n[10] run_forever — cycle 사이 wait 중 shutdown")
    event = threading.Event()
    runner, _ = _build_runner(
        watchlist=("005930",),
        shutdown_event=event,
    )
    # cycle_interval_sec = 2 (테스트용)
    
    def set_event_after(delay):
        time.sleep(delay)
        event.set()
    
    t = threading.Thread(target=set_event_after, args=(0.3,))
    t.start()
    
    start = time.monotonic()
    n = runner.run_forever(max_cycles=10)
    elapsed = time.monotonic() - start
    t.join()
    
    # 첫 사이클 + 짧은 대기 후 종료 → 2초 미만
    assert elapsed < 3.0, f"shutdown 응답 너무 느림: {elapsed:.2f}s"
    assert n >= 1
    print(f"   ✅ {n} 사이클 후 shutdown, elapsed={elapsed:.2f}s")


def test_multiple_symbols_summary():
    print("\n[11] 다종목 사이클 — per_symbol_summary")
    runner, audit_dir = _build_runner(
        watchlist=("005930", "000660"),
        min_interval=5,
    )
    result = runner.run_cycle()
    
    assert result.stats.total_symbols == 2
    # cycle audit 확인
    import json
    with (audit_dir / "cycles.jsonl").open("r", encoding="utf-8") as f:
        rec = json.loads(f.readline())
    assert len(rec["per_symbol_summary"]) >= 1  # 최소 1종목 처리됨
    symbols = [s["symbol"] for s in rec["per_symbol_summary"]]
    assert "005930" in symbols
    print(f"   ✅ per_symbol_summary: {symbols}")


def test_invalid_watchlist_symbols_filtered():
    print("\n[12] watchlist에 미상장 종목 포함 → 필터링")
    runner, _ = _build_runner(
        watchlist=("005930", "999999"),  # 999999 미상장
    )
    result = runner.run_cycle()
    # watchlist에서 999999 제거되어 005930만 처리
    assert result.stats.total_symbols == 1
    print(f"   ✅ 미상장 필터링: total={result.stats.total_symbols}")


def test_wait_seconds_zero():
    print("\n[13] _wait_seconds(0) — 즉시 반환")
    runner, _ = _build_runner(watchlist=("005930",))
    start = time.monotonic()
    runner._wait_seconds(0)
    elapsed = time.monotonic() - start
    assert elapsed < 0.05
    print(f"   ✅ 0초 대기: {elapsed*1000:.1f}ms")


if __name__ == "__main__":
    test_explicit_watchlist_required()
    test_min_interval_validation()
    test_single_cycle_one_symbol()
    test_cycle_min_interval_enforced()
    test_market_closed_skip()
    test_kill_switch_at_cycle_start()
    test_shutdown_event_during_interval()
    test_audit_log_written()
    test_run_forever_max_cycles()
    test_run_forever_shutdown_during_wait()
    test_multiple_symbols_summary()
    test_invalid_watchlist_symbols_filtered()
    test_wait_seconds_zero()
    print("\n🎉 모든 스모크 테스트 통과 (All smoke tests passed)")
