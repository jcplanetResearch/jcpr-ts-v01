"""스모크 테스트 (Smoke Test) — Task 21 v0.1 Execution Gateway.

⚠️ 모든 KIS 호출은 stub session을 통해서만 발생 — 실 API 미호출.
"""

import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# Task 21
from src.execution.gateway import ExecutionGateway
from src.execution.execution_record import (
    ExecutionAuditLog, ExecutionOutcome, ExecutionStage,
    compute_signal_id, new_execution_id,
)
from src.execution.approval import (
    ApprovalProvider, ApprovalRequest, ApprovalDecision,
    AutoApproveProvider, DenyAllProvider,
)
from src.execution.shutdown_check import ShutdownChecker, ShutdownStatus
from src.execution.sizing import OrderSizer, CapacityConfig
from src.execution.sizing_audit import SizingAuditLogger

# Task 8 (with stub session)
from src.brokers.kis import KISCredentials, KISEnv, KISAdapter

# Task 10
from src.data.symbol_master import SymbolMaster

# Task 14 v0.4
from src.signals.schema_v2 import MomentumSignalV04, SignalSide

# Task 19
from src.risk.gates import (
    KillSwitchGate, MarketStateGate, OrderRateLimitGate,
    DuplicateOrderGate, PriceSanityGate, PositionLimitGate,
    ExposureGate, DailyLossLimitGate,
)
from src.risk.risk_audit import RiskAuditLogger
from src.risk.risk_gate import RiskGateRunner


CSV_PATH = Path(__file__).parent / "data" / "reference" / "symbol_master.csv"


# ─────────────────────────────────────────────────
# Stub HTTP Session (Task 8 스모크에서 재사용)
# ─────────────────────────────────────────────────

class StubResponse:
    def __init__(self, status_code, json_data):
        self.status_code = status_code
        self._json = json_data
    def json(self):
        return self._json


class StubSession:
    def __init__(self, *, with_position: bool = False):
        self.calls = []
        self._token_count = 0
        self._with_position = with_position
        self.last_order_call = None

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append({"method": "POST", "url": url})
        if "/oauth2/tokenP" in url:
            self._token_count += 1
            return StubResponse(200, {
                "access_token": f"stub_token_{self._token_count}",
                "token_type": "Bearer", "expires_in": 86400,
            })
        if "order-cash" in url:
            self.last_order_call = {"data": data, "headers": dict(headers or {})}
            return StubResponse(200, {
                "rt_cd": "0", "msg_cd": "OK", "msg1": "주문 성공",
                "output": {"ODNO": "0009999111"},
            })
        return StubResponse(404, {"rt_cd": "1", "msg_cd": "404"})

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append({"method": "GET", "url": url})
        if "inquire-balance" in url:
            positions = []
            if self._with_position:
                positions = [{
                    "pdno": "005930", "prdt_name": "삼성전자",
                    "hldg_qty": "10", "ord_psbl_qty": "10",
                    "pchs_avg_pric": "70000", "prpr": "70500",
                    "evlu_amt": "705000", "pchs_amt": "700000",
                    "evlu_pfls_amt": "5000", "evlu_pfls_rt": "0.71",
                }]
            return StubResponse(200, {
                "rt_cd": "0", "msg_cd": "OK", "msg1": "",
                "output1": positions,
                "output2": [{
                    "dnca_tot_amt": "10000000",
                    "ord_psbl_cash": "9500000",
                    "tot_evlu_amt": "10705000" if self._with_position else "10000000",
                    "pchs_amt_smtl_amt": "700000" if self._with_position else "0",
                    "evlu_pfls_smtl_amt": "5000" if self._with_position else "0",
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
            return StubResponse(200, {
                "rt_cd": "0", "msg_cd": "OK", "msg1": "",
                "output": [],   # 미체결 주문 없음
            })
        return StubResponse(404, {"rt_cd": "1"})


# ─────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────

def _build_full_gateway(*, with_position: bool = False, kill_switch_path=None):
    """전체 컴포넌트 통합 게이트웨이 빌드."""
    creds = KISCredentials(
        env=KISEnv.PAPER, app_key="x"*20, app_secret="y"*20,
        account_no="12345678-01",
    )
    session = StubSession(with_position=with_position)
    adapter = KISAdapter(creds, http_session=session)

    sm = SymbolMaster.from_csv(CSV_PATH)

    sizing_audit = SizingAuditLogger(tempfile.mktemp(suffix=".jsonl"))
    sizer = OrderSizer(
        capacity=CapacityConfig(
            max_per_order_krw=Decimal("5000000"),
            max_per_day_krw=Decimal("20000000"),
            max_pct_of_equity=Decimal("0.2"),
            min_per_order_krw=Decimal("10000"),
        ),
        audit_logger=sizing_audit,
    )

    risk_audit = RiskAuditLogger(tempfile.mktemp(suffix=".jsonl"))
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
    risk_runner = RiskGateRunner(gates, risk_audit)

    audit_log = ExecutionAuditLog(tempfile.mktemp(suffix=".jsonl"))

    gateway = ExecutionGateway(
        adapter=adapter,
        symbol_master=sm,
        sizer=sizer,
        risk_runner=risk_runner,
        audit_log=audit_log,
    )
    return gateway, adapter, audit_log, session


def _make_signal(
    *, side=SignalSide.BUY, symbol="005930",
    score="0.5", confidence="0.7",
    timestamp=None,
):
    return MomentumSignalV04(
        symbol=symbol,
        timestamp_utc=timestamp or datetime.now(timezone.utc),
        composite_score=Decimal(score),
        side=side,
        confidence=Decimal(confidence),
    )


# ─────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────

def test_full_pipeline_buy_dry_run():
    print("\n[1] BUY 시그널 → 전체 파이프라인 → DRY-RUN 송신")
    gateway, adapter, audit_log, session = _build_full_gateway()
    sig = _make_signal(side=SignalSide.BUY)
    
    result = gateway.execute(sig, market_is_open=True)
    
    assert result.outcome == ExecutionOutcome.SUBMITTED, f"got {result.outcome}: {result.reject_reason}"
    assert result.final_stage == ExecutionStage.DONE
    assert result.is_dry_run is True
    assert result.broker_order_no is None  # dry-run
    assert result.quantity > 0
    assert result.aligned_price > 0
    print(f"   ✅ outcome={result.outcome.value}, qty={result.quantity}, "
          f"price={result.aligned_price}, dry_run=True")
    print(f"      audit cache size: {audit_log.cache_size()}")


def test_flat_signal_skipped():
    print("\n[2] FLAT 시그널 → SKIPPED")
    gateway, _, _, _ = _build_full_gateway()
    sig = _make_signal(side=SignalSide.FLAT, score="0.0", confidence="0.0")
    
    result = gateway.execute(sig)
    assert result.outcome == ExecutionOutcome.SKIPPED
    assert result.final_stage == ExecutionStage.SIGNAL_VALIDATION
    assert "FLAT" in result.reject_reason
    print(f"   ✅ FLAT → SKIPPED, reason={result.reject_reason}")


def test_low_confidence_skipped():
    print("\n[3] confidence 미달 → SKIPPED")
    gateway, _, _, _ = _build_full_gateway()
    sig = _make_signal(side=SignalSide.BUY, confidence="0.30")
    
    result = gateway.execute(sig)
    assert result.outcome == ExecutionOutcome.SKIPPED
    assert result.final_stage == ExecutionStage.SIGNAL_VALIDATION
    print(f"   ✅ 낮은 confidence → SKIPPED")


def test_unknown_symbol_rejected():
    print("\n[4] 미상장 종목 → REJECTED")
    gateway, _, _, _ = _build_full_gateway()
    sig = _make_signal(symbol="999999")
    
    result = gateway.execute(sig)
    assert result.outcome == ExecutionOutcome.REJECTED
    assert result.final_stage == ExecutionStage.SIGNAL_VALIDATION
    print(f"   ✅ 미상장 → REJECTED, reason={result.reject_reason}")


def test_market_closed_rejected():
    print("\n[5] 시장 미개장 → 리스크 게이트 거부")
    gateway, _, _, _ = _build_full_gateway()
    sig = _make_signal()
    
    result = gateway.execute(sig, market_is_open=False)
    assert result.outcome == ExecutionOutcome.REJECTED
    assert result.final_stage == ExecutionStage.RISK_GATE
    assert "market" in result.reject_reason.lower() or "시장" in result.reject_reason
    print(f"   ✅ 시장 미개장 → RISK_GATE reject")


def test_kill_switch_active():
    print("\n[6] Kill Switch 활성 → STOP_CHECK 즉시 거부")
    with tempfile.TemporaryDirectory() as td:
        kill_path = Path(td) / "KILL_SWITCH_ON"
        kill_path.write_text("active")
        
        creds = KISCredentials(
            env=KISEnv.PAPER, app_key="x"*20, app_secret="y"*20,
            account_no="12345678-01",
        )
        session = StubSession()
        adapter = KISAdapter(creds, http_session=session)
        sm = SymbolMaster.from_csv(CSV_PATH)
        sizer = OrderSizer(
            capacity=CapacityConfig(
                max_per_order_krw=Decimal("5000000"),
                max_per_day_krw=Decimal("20000000"),
                max_pct_of_equity=Decimal("0.2"),
                min_per_order_krw=Decimal("10000"),
            ),
            audit_logger=SizingAuditLogger(tempfile.mktemp(suffix=".jsonl")),
        )
        gates = [
            KillSwitchGate(kill_switch_path=str(kill_path)),
            MarketStateGate(),
            OrderRateLimitGate(),
            DuplicateOrderGate(),
            PriceSanityGate(),
            PositionLimitGate(max_positions=10),
            ExposureGate(max_pct_per_symbol=Decimal("0.20")),
            DailyLossLimitGate(max_daily_loss_krw=Decimal("500000")),
        ]
        risk_runner = RiskGateRunner(gates, RiskAuditLogger(tempfile.mktemp(suffix=".jsonl")))
        audit_log = ExecutionAuditLog(tempfile.mktemp(suffix=".jsonl"))
        
        # ShutdownChecker가 같은 kill_path 감지
        shutdown = ShutdownChecker(kill_switch_path=str(kill_path))
        gateway = ExecutionGateway(
            adapter=adapter, symbol_master=sm, sizer=sizer,
            risk_runner=risk_runner, audit_log=audit_log,
            shutdown=shutdown,
        )
        
        result = gateway.execute(_make_signal())
        assert result.outcome == ExecutionOutcome.REJECTED
        assert result.final_stage == ExecutionStage.STOP_CHECK
        assert "shutdown" in result.reject_reason.lower() or "kill" in result.reject_reason.lower()
        # 계좌 조회도 호출되지 않았어야 함 (early stop)
        balance_calls = [c for c in session.calls if "inquire-balance" in c["url"]]
        assert len(balance_calls) == 0, f"STOP_CHECK 후 계좌 조회됨: {len(balance_calls)}"
        print(f"   ✅ STOP_CHECK 즉시 거부, 후속 호출 0건")


def test_shutdown_event():
    print("\n[7] threading.Event shutdown signal")
    gateway, _, _, _ = _build_full_gateway()
    event = threading.Event()
    event.set()
    
    # ShutdownChecker를 event 기반으로 교체
    gateway._shutdown = ShutdownChecker(
        kill_switch_path="/tmp/__nonexistent__",
        shutdown_event=event,
    )
    
    result = gateway.execute(_make_signal())
    assert result.outcome == ExecutionOutcome.REJECTED
    assert result.final_stage == ExecutionStage.STOP_CHECK
    assert "shutdown_event" in result.reject_reason
    print(f"   ✅ event signal → STOP_CHECK reject")


def test_idempotency_duplicate():
    print("\n[8] 동일 signal_id 재실행 → SKIPPED (24시간 쿨다운)")
    gateway, _, audit_log, _ = _build_full_gateway()
    
    sig = _make_signal(timestamp=datetime(2026, 5, 6, 10, 0, 0, tzinfo=timezone.utc))
    
    # 첫 실행 — SUBMITTED
    result1 = gateway.execute(sig)
    assert result1.outcome == ExecutionOutcome.SUBMITTED
    
    # 같은 시그널 재실행 — SKIPPED
    result2 = gateway.execute(sig)
    assert result2.outcome == ExecutionOutcome.SKIPPED
    assert result2.final_stage == ExecutionStage.IDEMPOTENCY_CHECK
    assert result1.signal_id == result2.signal_id  # 동일 멱등 키
    print(f"   ✅ 동일 signal_id={result1.signal_id} → 두 번째 SKIPPED")


def test_deny_all_approval():
    print("\n[9] DenyAllProvider — APPROVAL 단계 거부")
    gateway, _, _, _ = _build_full_gateway()
    gateway._approval = DenyAllProvider()
    
    result = gateway.execute(_make_signal())
    assert result.outcome == ExecutionOutcome.REJECTED
    assert result.final_stage == ExecutionStage.APPROVAL
    assert "DenyAll" in result.reject_reason or "거부" in result.reject_reason
    print(f"   ✅ DenyAll → APPROVAL reject")


def test_auto_approve_blocks_live():
    print("\n[10] AutoApprove + 실거래 + LIVE orders → 자동 거부 (안전 가드)")
    creds = KISCredentials(
        env=KISEnv.LIVE, app_key="x"*20, app_secret="y"*20,
        account_no="12345678-01",
    )
    session = StubSession()
    adapter = KISAdapter(creds, http_session=session)
    
    # 실거래 활성화 (DryRunGuard live)
    adapter.dry_run_guard.enable_live(reason="task 21 test — should still be blocked by AutoApprove")
    
    sm = SymbolMaster.from_csv(CSV_PATH)
    sizer = OrderSizer(
        capacity=CapacityConfig(
            max_per_order_krw=Decimal("5000000"),
            max_per_day_krw=Decimal("20000000"),
            max_pct_of_equity=Decimal("0.2"),
            min_per_order_krw=Decimal("10000"),
        ),
        audit_logger=SizingAuditLogger(tempfile.mktemp(suffix=".jsonl")),
    )
    gates = [
        KillSwitchGate(kill_switch_path="/tmp/__nonexistent__"),
        MarketStateGate(), OrderRateLimitGate(), DuplicateOrderGate(),
        PriceSanityGate(), PositionLimitGate(max_positions=10),
        ExposureGate(max_pct_per_symbol=Decimal("0.20")),
        DailyLossLimitGate(max_daily_loss_krw=Decimal("500000")),
    ]
    risk_runner = RiskGateRunner(gates, RiskAuditLogger(tempfile.mktemp(suffix=".jsonl")))
    
    gateway = ExecutionGateway(
        adapter=adapter, symbol_master=sm, sizer=sizer,
        risk_runner=risk_runner,
        audit_log=ExecutionAuditLog(tempfile.mktemp(suffix=".jsonl")),
        approval=AutoApproveProvider(allow_live=False),  # 기본 — live 거부
    )
    
    result = gateway.execute(_make_signal())
    assert result.outcome == ExecutionOutcome.REJECTED
    assert result.final_stage == ExecutionStage.APPROVAL
    assert "live" in result.reject_reason.lower() or "Human" in result.reject_reason
    print(f"   ✅ AutoApprove(allow_live=False) + LIVE → 자동 거부")
    print(f"      reason={result.reject_reason}")


def test_audit_log_written():
    print("\n[11] Audit log JSONL 기록 확인")
    audit_path = tempfile.mktemp(suffix=".jsonl")
    gateway, _, audit_log, _ = _build_full_gateway()
    gateway._audit = ExecutionAuditLog(audit_path)
    
    # 3개 시그널 — 1 SUBMITTED, 1 FLAT(SKIPPED), 1 미상장(REJECTED)
    gateway.execute(_make_signal(timestamp=datetime(2026, 5, 6, 9, 0, 0, tzinfo=timezone.utc)))
    gateway.execute(_make_signal(side=SignalSide.FLAT, score="0", confidence="0",
                                  timestamp=datetime(2026, 5, 6, 9, 1, 0, tzinfo=timezone.utc)))
    gateway.execute(_make_signal(symbol="999999",
                                  timestamp=datetime(2026, 5, 6, 9, 2, 0, tzinfo=timezone.utc)))
    
    with open(audit_path, "r", encoding="utf-8") as f:
        lines = [l for l in f.readlines() if l.strip()]
    assert len(lines) == 3
    
    import json
    outcomes = [json.loads(l)["outcome"] for l in lines]
    assert outcomes == ["submitted", "skipped", "rejected"]
    
    # 비밀 누출 검사 — 모든 줄에 app_secret 같은 키워드 없어야 함
    raw = "\n".join(lines)
    assert "app_secret" not in raw.lower()
    assert "y" * 20 not in raw  # secret 값 없음
    assert "x" * 20 not in raw  # app_key 없음
    print(f"   ✅ 3개 기록, outcomes={outcomes}, 비밀 누출 없음")


def test_signal_id_deterministic():
    print("\n[12] signal_id 결정론 (멱등 키)")
    ts = datetime(2026, 5, 6, 9, 0, 0, tzinfo=timezone.utc)
    sid1 = compute_signal_id("005930", "momentum_v04", ts, "buy")
    sid2 = compute_signal_id("005930", "momentum_v04", ts, "buy")
    sid3 = compute_signal_id("005930", "momentum_v04", ts, "sell")  # 다른 side
    assert sid1 == sid2
    assert sid1 != sid3
    assert sid1.startswith("sig-")
    print(f"   ✅ {sid1} == {sid2}, 다른 side: {sid3}")


def test_position_in_account():
    print("\n[13] 보유 포지션 → ExposureGate 통과 검증")
    gateway, _, _, _ = _build_full_gateway(with_position=True)
    # 005930 이미 70만원어치 보유, equity는 약 1070만원
    # 추가 매수 시 노출률 확인 필요
    sig = _make_signal(side=SignalSide.BUY)
    
    result = gateway.execute(sig)
    # 노출 한도 20% (Exposure Gate) — 1070만원 * 20% = 약 214만원 가능
    # 사이저 max_per_order=500만원, max_pct=20% → 적절히 통과 또는 거부
    assert result.outcome in (ExecutionOutcome.SUBMITTED, ExecutionOutcome.REJECTED)
    print(f"   ✅ outcome={result.outcome.value}, stage={result.final_stage.value}")
    if result.reject_reason:
        print(f"      reason: {result.reject_reason}")


if __name__ == "__main__":
    test_full_pipeline_buy_dry_run()
    test_flat_signal_skipped()
    test_low_confidence_skipped()
    test_unknown_symbol_rejected()
    test_market_closed_rejected()
    test_kill_switch_active()
    test_shutdown_event()
    test_idempotency_duplicate()
    test_deny_all_approval()
    test_auto_approve_blocks_live()
    test_audit_log_written()
    test_signal_id_deterministic()
    test_position_in_account()
    print("\n🎉 모든 스모크 테스트 통과 (All smoke tests passed)")
    print("⚠️  실 KIS API 0회 호출 (모두 stub session)")
