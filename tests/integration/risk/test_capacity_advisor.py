"""Phase 2 A2-1 — capacity_advisor 단위 + 통합 테스트.

Test categories:
    - Ladder validation
    - SessionSignals validation
    - HistoryStats validation
    - Recommendation: positive PnL → up
    - Recommendation: negative PnL → down
    - Recommendation: flat PnL → hold
    - Recommendation: recon major → floor
    - Recommendation: recon missing → suppress up
    - Recommendation: recon minor → suppress up
    - Recommendation: exception_count > 0 → down
    - Recommendation: at top step → no further up
    - Recommendation: at floor → no further down
    - Recommendation: starting_capital not in ladder → normalize
    - History: consecutive_loss_days >= threshold → extra down
    - History: at floor → cannot extra down
    - rationale: 한국어 + non-empty
    - triggers: complete signal payload
    - immutability: CapacityRecommendation is frozen
    - timezone-aware: computed_at must have tzinfo
    - to_dict: JSON-serializable (Decimal → str)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.risk.capacity_advisor import (
    ALGORITHM_ID,
    CapacityAdvisor,
    CapacityRecommendation,
    HistoryStats,
    InvalidLadderError,
    SessionSignals,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def standard_ladder() -> tuple[Decimal, ...]:
    return (
        Decimal("1000000"),
        Decimal("2000000"),
        Decimal("5000000"),
        Decimal("10000000"),
        Decimal("20000000"),
    )


@pytest.fixture
def advisor(standard_ladder) -> CapacityAdvisor:
    return CapacityAdvisor(ladder=standard_ladder)


def _make_session(
    *,
    realized: str = "0",
    unrealized: str = "0",
    starting: str = "5000000",
    severity: str = "ok",
    exception_count: int = 0,
) -> SessionSignals:
    return SessionSignals(
        realized_pnl_krw=Decimal(realized),
        unrealized_pnl_krw=Decimal(unrealized),
        starting_capital_krw=Decimal(starting),
        reconciliation_severity=severity,  # type: ignore[arg-type]
        exception_count=exception_count,
    )


# ---------------------------------------------------------------------------
# Ladder validation
# ---------------------------------------------------------------------------


class TestLadderValidation:
    def test_empty_ladder_returns_unavailable(self):
        adv = CapacityAdvisor(ladder=())
        rec = adv.recommend(_make_session())
        assert rec.available is False
        assert rec.reason == "no_ladder"
        assert rec.recommended_capacity_krw is None
        assert rec.direction is None

    def test_unsorted_ladder_raises(self, standard_ladder):
        bad = list(standard_ladder)
        bad[2], bad[3] = bad[3], bad[2]  # swap to break ordering
        with pytest.raises(InvalidLadderError, match="strictly increasing"):
            CapacityAdvisor(ladder=tuple(bad))

    def test_non_decimal_ladder_raises(self):
        with pytest.raises(InvalidLadderError, match="must be Decimal"):
            CapacityAdvisor(ladder=(1000000, 2000000))  # type: ignore[arg-type]

    def test_negative_ladder_raises(self):
        with pytest.raises(InvalidLadderError, match="must be positive"):
            CapacityAdvisor(
                ladder=(Decimal("-1000"), Decimal("1000"))
            )

    def test_zero_ladder_value_raises(self):
        with pytest.raises(InvalidLadderError, match="must be positive"):
            CapacityAdvisor(ladder=(Decimal("0"), Decimal("1000")))

    def test_duplicate_ladder_raises(self):
        with pytest.raises(InvalidLadderError, match="strictly increasing"):
            CapacityAdvisor(
                ladder=(Decimal("1000"), Decimal("1000"), Decimal("2000"))
            )


# ---------------------------------------------------------------------------
# SessionSignals validation
# ---------------------------------------------------------------------------


class TestSessionSignalsValidation:
    def test_non_decimal_realized_raises(self):
        with pytest.raises(TypeError, match="must be Decimal"):
            SessionSignals(
                realized_pnl_krw=100,  # type: ignore[arg-type]
                unrealized_pnl_krw=Decimal("0"),
                starting_capital_krw=Decimal("1000000"),
                reconciliation_severity="ok",
                exception_count=0,
            )

    def test_zero_starting_capital_raises(self):
        with pytest.raises(ValueError, match="starting_capital_krw must be positive"):
            SessionSignals(
                realized_pnl_krw=Decimal("0"),
                unrealized_pnl_krw=Decimal("0"),
                starting_capital_krw=Decimal("0"),
                reconciliation_severity="ok",
                exception_count=0,
            )

    def test_negative_exception_count_raises(self):
        with pytest.raises(ValueError, match="exception_count"):
            _make_session(exception_count=-1)

    def test_invalid_severity_raises(self):
        with pytest.raises(ValueError, match="reconciliation_severity"):
            _make_session(severity="critical")  # type: ignore

    def test_total_pnl_property(self):
        s = _make_session(realized="1000", unrealized="500")
        assert s.total_pnl_krw == Decimal("1500")


# ---------------------------------------------------------------------------
# HistoryStats validation
# ---------------------------------------------------------------------------


class TestHistoryStatsValidation:
    def test_negative_sessions_count_raises(self):
        with pytest.raises(ValueError, match="sessions_count"):
            HistoryStats(
                sessions_count=-1,
                cumulative_realized_pnl_krw=Decimal("0"),
                max_drawdown_krw=Decimal("0"),
                consecutive_loss_days=0,
            )

    def test_negative_consecutive_loss_raises(self):
        with pytest.raises(ValueError, match="consecutive_loss_days"):
            HistoryStats(
                sessions_count=10,
                cumulative_realized_pnl_krw=Decimal("0"),
                max_drawdown_krw=Decimal("0"),
                consecutive_loss_days=-1,
            )

    def test_negative_max_drawdown_raises(self):
        with pytest.raises(ValueError, match="max_drawdown_krw"):
            HistoryStats(
                sessions_count=10,
                cumulative_realized_pnl_krw=Decimal("0"),
                max_drawdown_krw=Decimal("-100"),
                consecutive_loss_days=0,
            )


# ---------------------------------------------------------------------------
# Direction recommendations
# ---------------------------------------------------------------------------


class TestDirectionRecommendations:
    def test_positive_pnl_recon_ok_no_exception_goes_up(self, advisor):
        # 5,000,000 starting, +5% realized → step 2 → step 3
        s = _make_session(realized="250000", starting="5000000", severity="ok")
        rec = advisor.recommend(s)
        assert rec.available is True
        assert rec.direction == "up"
        assert rec.ladder_step_from == 2
        assert rec.ladder_step_to == 3
        assert rec.recommended_capacity_krw == Decimal("10000000")

    def test_negative_pnl_goes_down(self, advisor):
        # 5,000,000 starting, -5% realized → step 2 → step 1
        s = _make_session(realized="-250000", starting="5000000", severity="ok")
        rec = advisor.recommend(s)
        assert rec.direction == "down"
        assert rec.ladder_step_to == 1
        assert rec.recommended_capacity_krw == Decimal("2000000")

    def test_flat_pnl_holds(self, advisor):
        # 5,000,000 starting, +0.1% realized (within ±0.5% threshold) → hold
        s = _make_session(realized="5000", starting="5000000", severity="ok")
        rec = advisor.recommend(s)
        assert rec.direction == "hold"
        assert rec.ladder_step_to == rec.ladder_step_from

    def test_recon_major_forces_floor(self, advisor):
        # major 위반 → 모든 다른 신호 무시하고 step 0
        s = _make_session(realized="500000", starting="5000000", severity="major")
        rec = advisor.recommend(s)
        assert rec.direction == "floor"
        assert rec.ladder_step_to == 0
        assert rec.recommended_capacity_krw == Decimal("1000000")
        assert any("major" in m for m in rec.rationale)

    def test_recon_missing_suppresses_up(self, advisor):
        # 양의 P&L 이지만 recon 미실행 → 상승 무효화 → hold
        s = _make_session(realized="250000", starting="5000000", severity="missing")
        rec = advisor.recommend(s)
        assert rec.direction == "hold"
        assert rec.ladder_step_to == 2  # unchanged from from
        assert any("미실행" in m for m in rec.rationale)

    def test_recon_minor_suppresses_up(self, advisor):
        s = _make_session(realized="250000", starting="5000000", severity="minor")
        rec = advisor.recommend(s)
        assert rec.direction == "hold"
        assert any("minor" in m for m in rec.rationale)

    def test_exception_count_forces_down(self, advisor):
        # 양의 P&L 이지만 exception > 0 → 하강
        s = _make_session(
            realized="250000",
            starting="5000000",
            severity="ok",
            exception_count=2,
        )
        rec = advisor.recommend(s)
        assert rec.direction == "down"
        assert rec.ladder_step_to == 1


# ---------------------------------------------------------------------------
# Boundary conditions
# ---------------------------------------------------------------------------


class TestBoundaryConditions:
    def test_at_top_step_cannot_go_up(self, advisor, standard_ladder):
        # 최상단(20M)에서 양의 PnL → hold (상승 불가)
        s = _make_session(realized="500000", starting="20000000", severity="ok")
        rec = advisor.recommend(s)
        assert rec.direction == "hold"
        assert rec.ladder_step_from == 4
        assert rec.ladder_step_to == 4
        assert any("최상단" in m for m in rec.rationale)

    def test_at_floor_cannot_go_down(self, advisor):
        # 최하단(1M)에서 음의 PnL → hold (하강 불가)
        s = _make_session(realized="-50000", starting="1000000", severity="ok")
        rec = advisor.recommend(s)
        assert rec.direction == "hold"
        assert rec.ladder_step_from == 0
        assert rec.ladder_step_to == 0
        assert any("최하단" in m for m in rec.rationale)

    def test_starting_capital_not_in_ladder_normalizes(self, advisor):
        # 7,000,000은 ladder에 없음 — 가장 가까운 단계(5M 또는 10M)로 정규화
        s = _make_session(realized="0", starting="7000000", severity="ok")
        rec = advisor.recommend(s)
        # 7M → 5M(diff 2M) vs 10M(diff 3M) → step 2 (5M)
        assert rec.ladder_step_from == 2
        assert any("정규화" in m for m in rec.rationale)
        assert rec.triggers.get("normalized") is True


# ---------------------------------------------------------------------------
# History-based extra down
# ---------------------------------------------------------------------------


class TestHistoryAdjustments:
    def test_consecutive_loss_threshold_triggers_extra_down(self, advisor):
        s = _make_session(realized="0", starting="5000000", severity="ok")
        history = HistoryStats(
            sessions_count=10,
            cumulative_realized_pnl_krw=Decimal("-100000"),
            max_drawdown_krw=Decimal("100000"),
            consecutive_loss_days=3,
        )
        rec = advisor.recommend(s, history=history)
        # flat session + history triggers → extra down
        assert rec.direction == "down"
        assert rec.ladder_step_to == 1
        assert any("연속 손실" in m for m in rec.rationale)

    def test_history_below_threshold_no_extra_down(self, advisor):
        s = _make_session(realized="0", starting="5000000", severity="ok")
        history = HistoryStats(
            sessions_count=10,
            cumulative_realized_pnl_krw=Decimal("-50000"),
            max_drawdown_krw=Decimal("50000"),
            consecutive_loss_days=2,  # below default threshold (3)
        )
        rec = advisor.recommend(s, history=history)
        assert rec.direction == "hold"

    def test_history_at_floor_cannot_extra_down(self, advisor):
        s = _make_session(realized="0", starting="1000000", severity="ok")
        history = HistoryStats(
            sessions_count=10,
            cumulative_realized_pnl_krw=Decimal("-100000"),
            max_drawdown_krw=Decimal("100000"),
            consecutive_loss_days=5,
        )
        rec = advisor.recommend(s, history=history)
        # 이미 최하단이므로 추가 하향 불가
        assert rec.ladder_step_to == 0
        assert any("최하단으로 추가 하향 불가" in m for m in rec.rationale)


# ---------------------------------------------------------------------------
# Output structure & invariants
# ---------------------------------------------------------------------------


class TestOutputInvariants:
    def test_rationale_is_korean_and_non_empty(self, advisor):
        s = _make_session(realized="250000", starting="5000000", severity="ok")
        rec = advisor.recommend(s)
        assert len(rec.rationale) >= 1
        # 한국어 문자가 최소 하나의 메시지에 포함
        assert any(any("\uac00" <= c <= "\ud7af" for c in m) for m in rec.rationale)

    def test_triggers_contains_all_signals(self, advisor):
        s = _make_session(
            realized="100",
            unrealized="200",
            starting="5000000",
            severity="ok",
            exception_count=1,
        )
        rec = advisor.recommend(s)
        assert "pnl_signal" in rec.triggers
        assert "total_pnl_krw" in rec.triggers
        assert "starting_capital_krw" in rec.triggers
        assert "reconciliation_severity" in rec.triggers
        assert "exception_count" in rec.triggers
        assert "ladder_size" in rec.triggers

    def test_recommendation_is_frozen(self, advisor):
        s = _make_session()
        rec = advisor.recommend(s)
        with pytest.raises((AttributeError, Exception)):
            rec.available = False  # type: ignore[misc]

    def test_computed_at_is_timezone_aware(self, advisor):
        s = _make_session()
        rec = advisor.recommend(s)
        assert rec.computed_at.tzinfo is not None

    def test_algorithm_id_stable(self, advisor):
        s = _make_session()
        rec = advisor.recommend(s)
        assert rec.algorithm == "ladder_step_v1"
        assert rec.algorithm == ALGORITHM_ID

    def test_to_dict_is_json_serializable(self, advisor):
        s = _make_session(realized="250000", starting="5000000", severity="ok")
        rec = advisor.recommend(s)
        d = rec.to_dict()
        # 라운드트립
        json_str = json.dumps(d, ensure_ascii=False)
        loaded = json.loads(json_str)
        assert loaded["algorithm"] == "ladder_step_v1"
        assert loaded["available"] is True
        # Decimal은 문자열로 직렬화
        assert isinstance(loaded["recommended_capacity_krw"], str)

    def test_decimal_precision_preserved(self, advisor):
        # float 사용 금지 검증 — Decimal 정밀도 보존
        s = _make_session(realized="123.456", starting="5000000", severity="ok")
        rec = advisor.recommend(s)
        # total_pnl_krw 는 str 으로 저장되므로 정밀도 그대로
        assert rec.triggers["total_pnl_krw"] == "123.456"

    def test_now_injection_for_deterministic_tests(self, advisor):
        fixed = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
        s = _make_session()
        rec = advisor.recommend(s, now=fixed)
        assert rec.computed_at == fixed
