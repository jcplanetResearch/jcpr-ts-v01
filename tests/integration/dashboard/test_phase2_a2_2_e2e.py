"""Phase 2 A2-2 — End-to-end: writer → reader → capacity_advisor.

본 테스트는 A2-1 reader + A2-2 writer + capacity_advisor 의 통합을 검증.
writer 가 append 한 jsonl 이 reader 에 정상 인식되고, capacity_advisor 가
history 기반 조정을 정확히 수행하는지 확인.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from src.dashboard.views._session_history_reader import (
    HistoryReadResult,
    try_load_history,
)
from src.dashboard.views._session_history_writer import (
    SessionRecord,
    append_session_record,
    build_paper_session_record,
)
from src.risk.capacity_advisor import (
    CapacityAdvisor,
    HistoryStats,
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


def _append_n_records(
    audit_path: Path,
    n: int,
    *,
    realized_per_session: list[str],
    base_ts: datetime | None = None,
) -> list[SessionRecord]:
    """N 개 record 를 순차적으로 append. 반환은 record 리스트."""
    if base_ts is None:
        base_ts = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    records: list[SessionRecord] = []
    for i in range(n):
        ts = base_ts + timedelta(days=i)
        rec = build_paper_session_record(
            starting_capital_krw=Decimal("5000000"),
            ending_capital_krw=Decimal("5000000") + Decimal(realized_per_session[i]),
            realized_pnl_krw=Decimal(realized_per_session[i]),
            session_id=f"s{i:03d}",
            timestamp=ts,
        )
        append_session_record(audit_path, rec)
        records.append(rec)
    return records


# ---------------------------------------------------------------------------
# E2E: writer → reader round-trip
# ---------------------------------------------------------------------------


class TestWriterReaderRoundTrip:
    def test_single_record_round_trip(self, tmp_path):
        f = tmp_path / "sessions.jsonl"
        rec = build_paper_session_record(
            starting_capital_krw=Decimal("5000000"),
            ending_capital_krw=Decimal("5012345"),
            realized_pnl_krw=Decimal("12345"),
            session_id="s1",
            timestamp=datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc),
        )
        append_session_record(f, rec)

        # reader 로 다시 읽기
        result = try_load_history(f, days=365)
        assert result is not None
        assert result.sessions_count == 1
        assert result.cumulative_realized_pnl_krw == Decimal("12345")
        assert result.skipped_lines == 0

    def test_five_records_round_trip(self, tmp_path):
        f = tmp_path / "sessions.jsonl"
        # +100, -50, +200, -300, +400
        _append_n_records(
            f, 5,
            realized_per_session=["100", "-50", "200", "-300", "400"],
        )

        result = try_load_history(f, days=365)
        assert result is not None
        assert result.sessions_count == 5
        # 누적: 100 - 50 + 200 - 300 + 400 = 350
        assert result.cumulative_realized_pnl_krw == Decimal("350")

    def test_consecutive_loss_detected(self, tmp_path):
        f = tmp_path / "sessions.jsonl"
        # 마지막 3 세션이 모두 음 → consecutive_loss_days = 3
        _append_n_records(
            f, 5,
            realized_per_session=["100", "200", "-50", "-30", "-20"],
        )

        result = try_load_history(f, days=365)
        assert result is not None
        assert result.consecutive_loss_days == 3

    def test_max_drawdown_computed(self, tmp_path):
        f = tmp_path / "sessions.jsonl"
        # cumulative: 1000 → 2000 → 1500 → 500 → 700
        # peak=2000, trough=500, MDD=1500
        _append_n_records(
            f, 5,
            realized_per_session=["1000", "1000", "-500", "-1000", "200"],
        )

        result = try_load_history(f, days=365)
        assert result is not None
        assert result.max_drawdown_krw == Decimal("1500")

    def test_30day_cutoff_applies(self, tmp_path):
        f = tmp_path / "sessions.jsonl"
        # 60일 전 세션 1개 + 5일 전 세션 1개
        old = build_paper_session_record(
            starting_capital_krw=Decimal("5000000"),
            ending_capital_krw=Decimal("5001000"),
            realized_pnl_krw=Decimal("1000"),
            session_id="old",
            timestamp=datetime.now(timezone.utc) - timedelta(days=60),
        )
        recent = build_paper_session_record(
            starting_capital_krw=Decimal("5000000"),
            ending_capital_krw=Decimal("5002000"),
            realized_pnl_krw=Decimal("2000"),
            session_id="recent",
            timestamp=datetime.now(timezone.utc) - timedelta(days=5),
        )
        append_session_record(f, old)
        append_session_record(f, recent)

        # 30일 cutoff
        result = try_load_history(f, days=30)
        assert result is not None
        assert result.sessions_count == 1
        assert result.cumulative_realized_pnl_krw == Decimal("2000")


# ---------------------------------------------------------------------------
# E2E: writer → reader → capacity_advisor
# ---------------------------------------------------------------------------


class TestEndToEndAdvisor:
    def test_history_triggers_extra_down(self, tmp_path, advisor):
        """3일 연속 손실 → advisor 가 추가 1단계 하향."""
        f = tmp_path / "sessions.jsonl"
        _append_n_records(
            f, 5,
            realized_per_session=["100", "200", "-50", "-30", "-20"],
        )

        # reader → HistoryStats
        read_result = try_load_history(f, days=365)
        assert read_result is not None
        history = HistoryStats(
            sessions_count=read_result.sessions_count,
            cumulative_realized_pnl_krw=read_result.cumulative_realized_pnl_krw,
            max_drawdown_krw=read_result.max_drawdown_krw,
            consecutive_loss_days=read_result.consecutive_loss_days,
        )

        # 현재 세션은 flat — history 만으로 추가 하향이 발화해야 함
        session = SessionSignals(
            realized_pnl_krw=Decimal("0"),
            unrealized_pnl_krw=Decimal("0"),
            starting_capital_krw=Decimal("5000000"),
            reconciliation_severity="ok",
            exception_count=0,
        )
        rec = advisor.recommend(session, history=history)
        # flat session 단독 → hold (step 2). history 트리거 → down (step 1)
        assert rec.direction == "down"
        assert rec.ladder_step_to == 1

    def test_no_history_no_extra_adjustment(self, tmp_path, advisor):
        """history 부재 (jsonl 비어있음) → 단일 세션 모드 동작."""
        f = tmp_path / "sessions.jsonl"
        # writer 호출 없이 reader 시도
        read_result = try_load_history(f, days=365)
        assert read_result is None  # 파일 미존재

        session = SessionSignals(
            realized_pnl_krw=Decimal("250000"),
            unrealized_pnl_krw=Decimal("0"),
            starting_capital_krw=Decimal("5000000"),
            reconciliation_severity="ok",
            exception_count=0,
        )
        rec = advisor.recommend(session, history=None)
        # +5%, recon ok, no exc → 1단계 상승
        assert rec.direction == "up"
        assert rec.ladder_step_to == 3

    def test_short_consecutive_loss_no_extra_down(self, tmp_path, advisor):
        """연속 손실 < 3일 → 추가 하향 없음."""
        f = tmp_path / "sessions.jsonl"
        # 마지막 2일만 음
        _append_n_records(
            f, 5,
            realized_per_session=["100", "200", "300", "-50", "-30"],
        )

        read_result = try_load_history(f, days=365)
        assert read_result is not None
        assert read_result.consecutive_loss_days == 2

        history = HistoryStats(
            sessions_count=read_result.sessions_count,
            cumulative_realized_pnl_krw=read_result.cumulative_realized_pnl_krw,
            max_drawdown_krw=read_result.max_drawdown_krw,
            consecutive_loss_days=read_result.consecutive_loss_days,
        )
        session = SessionSignals(
            realized_pnl_krw=Decimal("0"),
            unrealized_pnl_krw=Decimal("0"),
            starting_capital_krw=Decimal("5000000"),
            reconciliation_severity="ok",
            exception_count=0,
        )
        rec = advisor.recommend(session, history=history)
        assert rec.direction == "hold"  # consecutive < 3, flat session


# ---------------------------------------------------------------------------
# Schema compatibility
# ---------------------------------------------------------------------------


class TestSchemaCompatibility:
    def test_writer_record_not_skipped_by_reader(self, tmp_path):
        """writer 가 append 한 record 가 reader skip 카운트를 증가시키지 않음."""
        f = tmp_path / "sessions.jsonl"
        _append_n_records(f, 3, realized_per_session=["100", "200", "300"])

        result = try_load_history(f, days=365)
        assert result is not None
        assert result.sessions_count == 3
        assert result.skipped_lines == 0
