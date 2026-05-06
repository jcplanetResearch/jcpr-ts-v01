"""Task 16 자체 검증 스위트 (Self-verification suite) — 46 checks.

카테고리별:
  Stage 0 cadence : 4
  Stage 0 stop    : 5
  Stage 1 filter  : 4
  Stage 2 dedup   : 4
  Stage 3 conflict: 6 (v0.2 — 동일 카테고리만 거부)
  Stage 4 sort    : 4
  Stage 4 supers. : 6 (R1 신규)
  Stage 5 resolve : 6
  Stage 6 emit    : 4
  End-to-end      : 3
  ------------------
  Total           : 46
"""
import sys
sys.path.insert(0, '/home/claude')

from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional

import pydantic

from src.risk import RejectionReason
from src.signals import (
    Signal, SignalAction, SignalStrength, SignalCategory, SignalBatch,
    CadenceTracker, RunnerStopReason, preflight_stop_check,
    RejectedSignal, RunnerDecision, StageMetrics,
    StubCapitalEstimator, FixedCapitalEstimator,
    SignalRunner, RUNNER_VERSION,
)
from src.signals._filter import filter_signals
from src.signals._dedup import dedup_signals
from src.signals._conflict import detect_conflicts
from src.signals._resolve import resolve_capital_conflict
from src.signals.runner import _apply_cross_category_supersession


# ============================================================
# 헬퍼
# ============================================================

class FakeStopState:
    """테스트용 StopState 구현체 — 모든 플래그 토글 가능."""
    def __init__(self, kill=False, emergency=False, keyboard=False):
        self.kill = kill
        self.emergency = emergency
        self.keyboard = keyboard
    
    def kill_switch_active(self) -> bool:
        return self.kill
    
    def emergency_stop_engaged(self) -> bool:
        return self.emergency
    
    def keyboard_stop_engaged(self) -> bool:
        return self.keyboard


def make_signal(
    symbol: str = "005930",
    action: SignalAction = SignalAction.BUY,
    category: SignalCategory = SignalCategory.ENTRY,
    strength: SignalStrength = SignalStrength.MEDIUM,
    inputs_hash: str = "h1",
    as_of: Optional[datetime] = None,
    expires_at: Optional[datetime] = None,
    confidence: Decimal = Decimal("0.5"),
    reference_price: Decimal = Decimal("70000"),
) -> Signal:
    if as_of is None:
        as_of = datetime.now(timezone.utc)
    return Signal(
        strategy_name="test_strategy",
        strategy_version="v1.0.0",
        symbol=symbol,
        action=action,
        strength=strength,
        signal_category=category,
        confidence=confidence,
        reference_price=reference_price,
        as_of_utc=as_of,
        created_at_utc=as_of,
        expires_at_utc=expires_at,
        inputs_hash=inputs_hash,
    )


def make_batch(signals: list, as_of: Optional[datetime] = None) -> SignalBatch:
    if as_of is None:
        as_of = datetime.now(timezone.utc)
    return SignalBatch(
        strategy_name="test_strategy",
        strategy_version="v1.0.0",
        as_of_utc=as_of,
        universe_size=len(signals),
        signals=tuple(signals),
    )


# ============================================================
# 검증 스위트
# ============================================================

results: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    results.append((name, passed, detail))
    icon = "✓" if passed else "✗"
    print(f"  {icon} {name}" + (f" — {detail}" if detail and not passed else ""))


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ============================================================
# Stage 0a — CADENCE (4)
# ============================================================
section("Stage 0a — Cadence (4 checks)")

# C1: 첫 호출 통과
tr = CadenceTracker(min_cycle_seconds=5)
now = datetime(2026, 5, 5, 0, 0, 0, tzinfo=timezone.utc)
r = tr.check_and_advance(now)
check("C1 cadence: 첫 호출 통과", r.allowed and r.elapsed_seconds is None)

# C2: <5초 거부
r2 = tr.check_and_advance(now + timedelta(seconds=3))
check("C2 cadence: 3초 후 호출 거부",
      not r2.allowed and r2.elapsed_seconds == 3.0 and r2.next_allowed_at is not None)

# C3: 정확히 5초 통과 (last_cycle 미갱신 후 5초 시도)
r3 = tr.check_and_advance(now + timedelta(seconds=5))
check("C3 cadence: 정확히 5초 후 통과",
      r3.allowed and r3.elapsed_seconds == 5.0)

# C4: tz-naive 거부
try:
    tr.check_and_advance(datetime(2026, 5, 5, 0, 0, 10))
    check("C4 cadence: tz-naive 거부", False, "ValueError 미발생")
except ValueError:
    check("C4 cadence: tz-naive 거부", True)

# ============================================================
# Stage 0b — STOP (5)
# ============================================================
section("Stage 0b — Stop preflight (5 checks)")

# S1: 모두 비활성 → None
check("S1 stop: 모두 비활성 → None",
      preflight_stop_check(FakeStopState()) is None)

# S2: kill_switch 활성
check("S2 stop: KILL_SWITCH 감지",
      preflight_stop_check(FakeStopState(kill=True)) == RunnerStopReason.KILL_SWITCH)

# S3: emergency 활성
check("S3 stop: EMERGENCY_STOP 감지",
      preflight_stop_check(FakeStopState(emergency=True)) == RunnerStopReason.EMERGENCY_STOP)

# S4: keyboard 활성
check("S4 stop: KEYBOARD_STOP 감지",
      preflight_stop_check(FakeStopState(keyboard=True)) == RunnerStopReason.KEYBOARD_STOP)

# S5: 우선순위 — kill > emergency > keyboard
check("S5 stop: kill > emergency > keyboard 우선순위",
      preflight_stop_check(FakeStopState(kill=True, emergency=True, keyboard=True))
      == RunnerStopReason.KILL_SWITCH)


# ============================================================
# Stage 1 — FILTER (4)
# ============================================================
section("Stage 1 — Filter (4 checks)")

now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)

# F1: 정상 통과
s_ok = make_signal(as_of=now, expires_at=now + timedelta(minutes=5))
acc, rej = filter_signals([s_ok], now)
check("F1 filter: 정상 통과",
      len(acc) == 1 and len(rej) == 0)

# F2: 만료 거부
s_exp = make_signal(as_of=now - timedelta(minutes=10),
                    expires_at=now - timedelta(minutes=1))
acc, rej = filter_signals([s_exp], now)
check("F2 filter: 만료 거부",
      len(acc) == 0 and len(rej) == 1
      and rej[0].reason == RejectionReason.SIGNAL_EXPIRED)

# F3: expires_at_utc=None 통과
s_no_exp = make_signal(as_of=now, expires_at=None)
acc, rej = filter_signals([s_no_exp], now)
check("F3 filter: expires_at=None 통과",
      len(acc) == 1 and len(rej) == 0)

# F4: 다수 만료/통과 혼재 — 독립 처리
s_mix1 = make_signal(symbol="A", as_of=now, expires_at=now + timedelta(minutes=5),
                     inputs_hash="ha")
s_mix2 = make_signal(symbol="B", as_of=now - timedelta(minutes=10),
                     expires_at=now - timedelta(seconds=1), inputs_hash="hb")
s_mix3 = make_signal(symbol="C", as_of=now, expires_at=None, inputs_hash="hc")
acc, rej = filter_signals([s_mix1, s_mix2, s_mix3], now)
check("F4 filter: 혼재 독립 처리",
      len(acc) == 2 and len(rej) == 1 and rej[0].signal.symbol == "B")


# ============================================================
# Stage 2 — DEDUP (4)
# ============================================================
section("Stage 2 — Dedup (4 checks)")

# D1: 동일 (hash, category) 중복 제거
s_d1 = make_signal(inputs_hash="hash_x", as_of=now, category=SignalCategory.ENTRY)
s_d2 = make_signal(inputs_hash="hash_x", as_of=now + timedelta(seconds=1),
                   category=SignalCategory.ENTRY)
acc, rej = dedup_signals([s_d1, s_d2])
check("D1 dedup: 동일 (hash, category) 중복 제거",
      len(acc) == 1 and len(rej) == 1
      and rej[0].reason == RejectionReason.DUPLICATE_SIGNAL)

# D2: 다른 category 동일 hash → 둘 다 보존 (의도 다름)
s_d3 = make_signal(inputs_hash="hash_y", category=SignalCategory.STOP_LOSS,
                   action=SignalAction.SELL)
s_d4 = make_signal(inputs_hash="hash_y", category=SignalCategory.ENTRY,
                   action=SignalAction.BUY)
acc, rej = dedup_signals([s_d3, s_d4])
check("D2 dedup: 다른 category 동일 hash 보존",
      len(acc) == 2 and len(rej) == 0)

# D3: 다른 hash 동일 category → 둘 다 보존
s_d5 = make_signal(inputs_hash="hash_a")
s_d6 = make_signal(inputs_hash="hash_b")
acc, rej = dedup_signals([s_d5, s_d6])
check("D3 dedup: 다른 hash 동일 category 보존",
      len(acc) == 2 and len(rej) == 0)

# D4: 빈 입력
acc, rej = dedup_signals([])
check("D4 dedup: 빈 입력 처리",
      len(acc) == 0 and len(rej) == 0)


# ============================================================
# Stage 3 — CONFLICT v0.2 (6)
# ============================================================
section("Stage 3 — Conflict v0.2 (6 checks, 동일 카테고리만)")

# Co1: 동일 (symbol, category) BUY+SELL → 양쪽 REJECT
s_c1 = make_signal(symbol="005930", category=SignalCategory.ENTRY,
                   action=SignalAction.BUY, inputs_hash="cs1")
s_c2 = make_signal(symbol="005930", category=SignalCategory.ENTRY,
                   action=SignalAction.SELL, inputs_hash="cs2")
acc, rej = detect_conflicts([s_c1, s_c2])
check("Co1 conflict: 동일 (symbol, category) BUY+SELL → 양쪽 REJECT",
      len(acc) == 0 and len(rej) == 2
      and all(r.reason == RejectionReason.CONFLICTING_SIGNALS for r in rej))

# Co2: 동일 (symbol, category) BUY+CLOSE → 양쪽 REJECT
s_c3 = make_signal(symbol="005930", category=SignalCategory.ENTRY,
                   action=SignalAction.BUY, inputs_hash="cs3")
s_c4 = make_signal(symbol="005930", category=SignalCategory.ENTRY,
                   action=SignalAction.CLOSE, inputs_hash="cs4")
acc, rej = detect_conflicts([s_c3, s_c4])
check("Co2 conflict: 동일 category BUY+CLOSE → 양쪽 REJECT",
      len(acc) == 0 and len(rej) == 2)

# Co3: 다른 symbol → 둘 다 통과
s_c5 = make_signal(symbol="005930", action=SignalAction.BUY, inputs_hash="cs5")
s_c6 = make_signal(symbol="000660", action=SignalAction.SELL, inputs_hash="cs6")
acc, rej = detect_conflicts([s_c5, s_c6])
check("Co3 conflict: 다른 symbol → 둘 다 통과",
      len(acc) == 2 and len(rej) == 0)

# Co4: v0.2 핵심 — 다른 category STOP_LOSS-SELL + ENTRY-BUY → Stage 3 통과 (Stage 4 처리)
s_c7 = make_signal(symbol="005930", category=SignalCategory.STOP_LOSS,
                   action=SignalAction.SELL, inputs_hash="cs7")
s_c8 = make_signal(symbol="005930", category=SignalCategory.ENTRY,
                   action=SignalAction.BUY, inputs_hash="cs8")
acc, rej = detect_conflicts([s_c7, s_c8])
check("Co4 conflict v0.2: 다른 category STOP_LOSS+ENTRY → Stage 3 통과",
      len(acc) == 2 and len(rej) == 0)

# Co5: HOLD only → 통과
s_c9 = make_signal(symbol="005930", action=SignalAction.HOLD, inputs_hash="cs9")
acc, rej = detect_conflicts([s_c9])
check("Co5 conflict: HOLD only → 통과",
      len(acc) == 1 and len(rej) == 0)

# Co6: 동일 (symbol, category) BUY+BUY → 통과
s_c10 = make_signal(symbol="005930", category=SignalCategory.ENTRY,
                    action=SignalAction.BUY, inputs_hash="cs10")
s_c11 = make_signal(symbol="005930", category=SignalCategory.ENTRY,
                    action=SignalAction.BUY, inputs_hash="cs11")
acc, rej = detect_conflicts([s_c10, s_c11])
check("Co6 conflict: 동일 (symbol, category) 같은 방향 → 통과",
      len(acc) == 2 and len(rej) == 0)


# ============================================================
# Stage 4a — SORT (4)
# ============================================================
section("Stage 4 — Sort (4 checks)")

# St1: priority 1→4 정렬
def priority_check_sort(signals):
    return sorted(signals, key=lambda s: (s.priority(), s.as_of_utc, s.signal_id))

t0 = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)
s_p4 = make_signal(symbol="A", category=SignalCategory.ENTRY, as_of=t0, inputs_hash="hp1")  # pri 4
s_p1 = make_signal(symbol="B", category=SignalCategory.STOP_LOSS,
                   action=SignalAction.SELL, as_of=t0, inputs_hash="hp2")  # pri 1
s_p2 = make_signal(symbol="C", category=SignalCategory.EXIT,
                   action=SignalAction.SELL, as_of=t0, inputs_hash="hp3")  # pri 2
sorted_s = priority_check_sort([s_p4, s_p1, s_p2])
check("St1 sort: priority 1→2→4 정렬",
      [s.signal_category for s in sorted_s] ==
      [SignalCategory.STOP_LOSS, SignalCategory.EXIT, SignalCategory.ENTRY])

# St2: 동일 priority FCFS (as_of_utc 빠른 순)
s_a = make_signal(symbol="X", category=SignalCategory.ENTRY,
                  as_of=t0 + timedelta(seconds=2), inputs_hash="ha")
s_b = make_signal(symbol="Y", category=SignalCategory.ENTRY,
                  as_of=t0, inputs_hash="hb")
sorted_s = priority_check_sort([s_a, s_b])
check("St2 sort: 동일 priority FCFS",
      sorted_s[0].symbol == "Y")

# St3: 빈 입력
check("St3 sort: 빈 입력", priority_check_sort([]) == [])

# St4: STOP_LOSS 항상 최선두 (다양 priority 혼재)
mixed = [
    make_signal(symbol="A", category=SignalCategory.REBALANCE, action=SignalAction.BUY,
                as_of=t0, inputs_hash="ma"),  # pri 3
    make_signal(symbol="B", category=SignalCategory.STOP_LOSS, action=SignalAction.SELL,
                as_of=t0 + timedelta(seconds=10), inputs_hash="mb"),  # pri 1, 늦음
    make_signal(symbol="C", category=SignalCategory.ENTRY,
                as_of=t0, inputs_hash="mc"),  # pri 4
]
sorted_s = priority_check_sort(mixed)
check("St4 sort: STOP_LOSS 항상 최선두 (시간 늦어도)",
      sorted_s[0].signal_category == SignalCategory.STOP_LOSS)


# ============================================================
# Stage 4 — SUPERSESSION v0.2 (R1 신규, 6)
# ============================================================
section("Stage 4 — Cross-category supersession R1 (6 checks)")

# Sup1: STOP_LOSS-SELL + ENTRY-BUY 동일 symbol → STOP_LOSS 통과, ENTRY supersede
s_sup1 = make_signal(symbol="005930", category=SignalCategory.STOP_LOSS,
                     action=SignalAction.SELL, inputs_hash="sup1")
s_sup2 = make_signal(symbol="005930", category=SignalCategory.ENTRY,
                     action=SignalAction.BUY, inputs_hash="sup2")
acc, rej = _apply_cross_category_supersession((s_sup1, s_sup2))
check("Sup1 supersession: STOP_LOSS 통과, ENTRY LOWER_PRIORITY",
      len(acc) == 1 and acc[0].signal_category == SignalCategory.STOP_LOSS
      and len(rej) == 1 and rej[0].reason == RejectionReason.LOWER_PRIORITY
      and rej[0].metadata.get("superseder_category") == "STOP_LOSS"
      and rej[0].stage == 4)

# Sup2: EXIT-SELL + ENTRY-BUY → EXIT 통과
s_sup3 = make_signal(symbol="005930", category=SignalCategory.EXIT,
                     action=SignalAction.SELL, inputs_hash="sup3")
s_sup4 = make_signal(symbol="005930", category=SignalCategory.ENTRY,
                     action=SignalAction.BUY, inputs_hash="sup4")
acc, rej = _apply_cross_category_supersession((s_sup3, s_sup4))
check("Sup2 supersession: EXIT 우선, ENTRY supersede",
      len(acc) == 1 and acc[0].signal_category == SignalCategory.EXIT
      and len(rej) == 1)

# Sup3: REBALANCE-BUY + ENTRY-BUY (같은 방향) → 둘 다 통과
s_sup5 = make_signal(symbol="005930", category=SignalCategory.REBALANCE,
                     action=SignalAction.BUY, inputs_hash="sup5")
s_sup6 = make_signal(symbol="005930", category=SignalCategory.ENTRY,
                     action=SignalAction.BUY, inputs_hash="sup6")
acc, rej = _apply_cross_category_supersession((s_sup5, s_sup6))
check("Sup3 supersession: 동방향 다중 카테고리 둘 다 통과",
      len(acc) == 2 and len(rej) == 0)

# Sup4: REBALANCE-SELL + ENTRY-BUY (양방향, 다른 priority)
#   priority 3 (REBALANCE) vs priority 4 (ENTRY) → REBALANCE 우선
s_sup7 = make_signal(symbol="005930", category=SignalCategory.REBALANCE,
                     action=SignalAction.SELL, inputs_hash="sup7")
s_sup8 = make_signal(symbol="005930", category=SignalCategory.ENTRY,
                     action=SignalAction.BUY, inputs_hash="sup8")
acc, rej = _apply_cross_category_supersession((s_sup7, s_sup8))
check("Sup4 supersession: REBALANCE 우선, ENTRY supersede",
      len(acc) == 1 and acc[0].signal_category == SignalCategory.REBALANCE
      and len(rej) == 1)

# Sup5: 다른 symbol — supersession 미적용
s_sup9 = make_signal(symbol="005930", category=SignalCategory.STOP_LOSS,
                     action=SignalAction.SELL, inputs_hash="sup9")
s_sup10 = make_signal(symbol="000660", category=SignalCategory.ENTRY,
                      action=SignalAction.BUY, inputs_hash="sup10")
acc, rej = _apply_cross_category_supersession((s_sup9, s_sup10))
check("Sup5 supersession: 다른 symbol → 둘 다 통과",
      len(acc) == 2 and len(rej) == 0)

# Sup6: 동일 priority + 양방향 + 다른 카테고리 (이례적, fail-closed)
#   STOP_LOSS-SELL + RISK_REDUCE-BUY (둘 다 priority 1)
s_sup11 = make_signal(symbol="005930", category=SignalCategory.STOP_LOSS,
                      action=SignalAction.SELL, inputs_hash="sup11")
s_sup12 = make_signal(symbol="005930", category=SignalCategory.RISK_REDUCE,
                      action=SignalAction.BUY, inputs_hash="sup12")
acc, rej = _apply_cross_category_supersession((s_sup11, s_sup12))
check("Sup6 supersession: 동priority 양방향 다른 category → fail-closed 양쪽 REJECT",
      len(acc) == 0 and len(rej) == 2
      and all(r.reason == RejectionReason.CONFLICTING_SIGNALS for r in rej))


# ============================================================
# Stage 5 — RESOLVE (6)
# ============================================================
section("Stage 5 — Resolve (6 checks)")

est_fixed = FixedCapitalEstimator(Decimal("100000"))
est_stub = StubCapitalEstimator(default_qty=10)

# R1: 자본 충분 → 모두 통과
s_r1 = make_signal(symbol="A", action=SignalAction.BUY, inputs_hash="hr1",
                   reference_price=Decimal("70000"))
s_r2 = make_signal(symbol="B", action=SignalAction.BUY, inputs_hash="hr2",
                   reference_price=Decimal("70000"))
sorted_input = priority_check_sort([s_r1, s_r2])
acc, rej, consumed = resolve_capital_conflict(
    sorted_input, Decimal("10000000"), est_fixed)
check("R1 resolve: 자본 충분 → 모두 통과",
      len(acc) == 2 and len(rej) == 0 and consumed == Decimal("200000"))

# R2: 자본 부족 → 후순위 LOWER_PRIORITY
s_r3 = make_signal(symbol="A", action=SignalAction.BUY,
                   category=SignalCategory.STOP_LOSS,
                   inputs_hash="hr3")  # priority 1
s_r4 = make_signal(symbol="B", action=SignalAction.BUY,
                   category=SignalCategory.ENTRY,
                   inputs_hash="hr4")  # priority 4
sorted_input = priority_check_sort([s_r3, s_r4])
# 자본 100000 — 첫 시그널만 가능
acc, rej, consumed = resolve_capital_conflict(
    sorted_input, Decimal("100000"), est_fixed)
check("R2 resolve: 자본 부족 시 후순위 LOWER_PRIORITY",
      len(acc) == 1 and acc[0].signal_category == SignalCategory.STOP_LOSS
      and len(rej) == 1 and rej[0].reason == RejectionReason.LOWER_PRIORITY)

# R3: STOP_LOSS 항상 통과 (priority 우선 확인)
mix = [
    make_signal(symbol="A", category=SignalCategory.ENTRY, inputs_hash="ma"),  # pri 4
    make_signal(symbol="B", category=SignalCategory.ENTRY, inputs_hash="mb"),  # pri 4
    make_signal(symbol="C", category=SignalCategory.STOP_LOSS,
                action=SignalAction.SELL, inputs_hash="mc"),  # pri 1
]
sorted_mix = priority_check_sort(mix)
# StubEstimator 에서 SELL=0 이므로 STOP_LOSS 는 무료 — 항상 통과
acc, rej, consumed = resolve_capital_conflict(
    sorted_mix, Decimal("0"), est_stub)
# C(SELL)=0, A(BUY)=70000*0.5*2*10=700000, B 동일 → 자본 0 이면 SELL 만 통과
check("R3 resolve: STOP_LOSS-SELL 자본 0 에서도 통과 (SELL=0 cost)",
      any(s.signal_category == SignalCategory.STOP_LOSS for s in acc))

# R4: FCFS tie-break (동 priority + 동 cost)
t0 = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)
s_r5 = make_signal(symbol="A", category=SignalCategory.ENTRY,
                   action=SignalAction.BUY, as_of=t0, inputs_hash="hr5")
s_r6 = make_signal(symbol="B", category=SignalCategory.ENTRY,
                   action=SignalAction.BUY, as_of=t0 + timedelta(seconds=2),
                   inputs_hash="hr6")
sorted_input = priority_check_sort([s_r6, s_r5])  # 일부러 역순 입력
# 한 개만 가능한 자본
acc, rej, consumed = resolve_capital_conflict(
    sorted_input, Decimal("100000"), est_fixed)
check("R4 resolve: 동 priority FCFS — 빠른 as_of_utc 우선",
      len(acc) == 1 and acc[0].symbol == "A")

# R5: 자본 0 + 모두 BUY → 모두 거부 (StubEstimator BUY > 0)
s_r7 = make_signal(symbol="A", action=SignalAction.BUY, inputs_hash="hr7")
acc, rej, consumed = resolve_capital_conflict(
    [s_r7], Decimal("0"), est_stub)
check("R5 resolve: 자본 0 + BUY → 모두 거부",
      len(acc) == 0 and len(rej) == 1)

# R6: available_capital < 0 → ValueError
try:
    resolve_capital_conflict([], Decimal("-1"), est_fixed)
    check("R6 resolve: 음수 자본 거부", False)
except ValueError:
    check("R6 resolve: 음수 자본 거부", True)


# ============================================================
# Stage 6 — EMIT (4)
# ============================================================
section("Stage 6 — Emit (4 checks)")

# E1: RunnerDecision frozen
rd = RunnerDecision(
    cycle_id="cyc-1",
    as_of_utc=datetime.now(timezone.utc),
    input_batch_id="b1",
    input_strategy_name="t",
    input_strategy_version="v1",
)
try:
    rd.cycle_id = "x"  # type: ignore[misc]
    check("E1 emit: RunnerDecision frozen", False)
except (ValueError, TypeError, pydantic.ValidationError, AttributeError):
    check("E1 emit: RunnerDecision frozen", True)

# E2: __repr__ 시크릿 미포함
batch_with_meta = make_signal(inputs_hash="not_a_secret_hash")
rs = RejectedSignal(signal=batch_with_meta, reason=RejectionReason.SIGNAL_EXPIRED,
                    stage=1, metadata={"reason_detail": "expired now"})
check("E2 emit: RejectedSignal __repr__ 안전",
      "secret" not in repr(rs).lower() and "password" not in repr(rs).lower())

# E3: metadata 시크릿 차단
try:
    RejectedSignal(signal=batch_with_meta, reason=RejectionReason.SIGNAL_EXPIRED,
                   stage=1, metadata={"api_key": "x"})
    check("E3 emit: metadata api_key 차단", False)
except (ValueError, pydantic.ValidationError):
    check("E3 emit: metadata api_key 차단", True)

# E4: stage out > in 거부
try:
    StageMetrics(stage_1_filter_in=2, stage_1_filter_out=5)
    check("E4 emit: stage out>in 거부", False)
except (ValueError, pydantic.ValidationError):
    check("E4 emit: stage out>in 거부", True)


# ============================================================
# End-to-End — RUNNER (3)
# ============================================================
section("End-to-end — SignalRunner (3 checks)")

# E2E1: 정상 흐름
runner = SignalRunner(
    stop_state=FakeStopState(),
    capital_estimator=FixedCapitalEstimator(Decimal("50000")),
)
t1 = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)
batch1 = make_batch([
    make_signal(symbol="A", action=SignalAction.BUY, inputs_hash="e2e_a"),
    make_signal(symbol="B", action=SignalAction.BUY, inputs_hash="e2e_b"),
], as_of=t1)
decision = runner.run_cycle(batch1, Decimal("1000000"), t1)
check("E2E1: 정상 흐름 — 시그널 전부 통과",
      decision.is_actionable() and len(decision.accepted_signals) == 2
      and not decision.stop_engaged and not decision.cadence_violation)

# E2E2: 비상 정지 활성 — 즉시 STOP
runner_stop = SignalRunner(
    stop_state=FakeStopState(emergency=True),
    capital_estimator=FixedCapitalEstimator(Decimal("0")),
)
t2 = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)
batch2 = make_batch([
    make_signal(symbol="A", inputs_hash="stop_a"),
], as_of=t2)
decision_stop = runner_stop.run_cycle(batch2, Decimal("0"), t2)
check("E2E2: 비상 정지 활성 — 즉시 STOP, 모든 시그널 거부",
      decision_stop.stop_engaged
      and decision_stop.stop_reason == RunnerStopReason.EMERGENCY_STOP
      and len(decision_stop.accepted_signals) == 0
      and len(decision_stop.rejected_signals) == 1
      and decision_stop.rejected_signals[0].reason == RejectionReason.EMERGENCY_STOP_ACTIVE)

# E2E3: 통합 시나리오 — 만료 + 중복 + 충돌 + supersession + 자본부족
runner_int = SignalRunner(
    stop_state=FakeStopState(),
    capital_estimator=FixedCapitalEstimator(Decimal("100000")),
)
t3 = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)
sigs = [
    # 만료
    make_signal(symbol="A", inputs_hash="i_exp",
                as_of=t3 - timedelta(minutes=10),
                expires_at=t3 - timedelta(seconds=1)),
    # 중복 (동일 hash, category)
    make_signal(symbol="B", inputs_hash="i_dup", as_of=t3),
    make_signal(symbol="B", inputs_hash="i_dup", as_of=t3 + timedelta(seconds=1)),
    # 충돌 (동일 symbol+category, 양방향)
    make_signal(symbol="C", inputs_hash="i_c1", action=SignalAction.BUY,
                category=SignalCategory.ENTRY, as_of=t3),
    make_signal(symbol="C", inputs_hash="i_c2", action=SignalAction.SELL,
                category=SignalCategory.ENTRY, as_of=t3),
    # supersession (다른 category, 양방향)
    make_signal(symbol="D", inputs_hash="i_sup1", action=SignalAction.SELL,
                category=SignalCategory.STOP_LOSS, as_of=t3),
    make_signal(symbol="D", inputs_hash="i_sup2", action=SignalAction.BUY,
                category=SignalCategory.ENTRY, as_of=t3),
    # 자본 부족 후보 (다른 symbol, 다른 hash)
    make_signal(symbol="E", inputs_hash="i_e1", action=SignalAction.BUY),
    make_signal(symbol="F", inputs_hash="i_f1", action=SignalAction.BUY),
]
batch3 = make_batch(sigs, as_of=t3)
decision_int = runner_int.run_cycle(batch3, Decimal("100000"), t3)

# 검증: 다양한 거부 사유 모두 등장
rejection_reasons = {r.reason for r in decision_int.rejected_signals}
expected_reasons = {
    RejectionReason.SIGNAL_EXPIRED,
    RejectionReason.DUPLICATE_SIGNAL,
    RejectionReason.CONFLICTING_SIGNALS,
    RejectionReason.LOWER_PRIORITY,
}
check("E2E3: 통합 시나리오 — 4가지 거부 사유 모두 발생",
      expected_reasons.issubset(rejection_reasons),
      detail=f"발견된 사유: {[r.value for r in rejection_reasons]}")


# ============================================================
# 결과 집계
# ============================================================
print("\n" + "=" * 60)
total = len(results)
passed = sum(1 for _, p, _ in results if p)
failed = total - passed
print(f"  결과: {passed}/{total} 통과 ({passed/total*100:.1f}%)")
if failed > 0:
    print(f"\n  실패 항목:")
    for name, p, detail in results:
        if not p:
            print(f"    ✗ {name} — {detail}")
    sys.exit(1)
else:
    print("  ✅ 모든 검사 통과 (no defects)")
    sys.exit(0)
