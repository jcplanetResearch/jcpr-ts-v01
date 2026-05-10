"""Phase 2 A2-2 — session_history_writer 단위 테스트.

Test categories:
    - SessionRecord validation (Decimal, tzinfo, severity, mode, etc.)
    - File creation: missing → 0600
    - Permission enforcement: 0600 OK, 0644 reject
    - JSON serialization (Decimal precision)
    - Atomic append (multiple records → multiple lines)
    - Parent directory auto-creation
    - Helper builders (build_paper_session_record, build_live_session_record)
    - Default value handling (session_id auto, timestamp auto, etc.)
    - try_append_session_record (best-effort wrapper)
    - Record size limit (PIPE_BUF safety)
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from src.dashboard.views._session_history_writer import (
    MAX_RECORD_LINE_BYTES,
    SCHEMA_VERSION,
    SessionHistoryWriteError,
    SessionHistoryWritePermissionError,
    SessionHistoryWriteRecordError,
    SessionRecord,
    append_session_record,
    build_live_session_record,
    build_paper_session_record,
    try_append_session_record,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_record(
    *,
    session_id: str = "2026-05-10_120000",
    realized: str = "10000",
    mode: str = "paper",
    severity: str = "ok",
    exception_count: int = 0,
) -> SessionRecord:
    return SessionRecord(
        session_id=session_id,
        timestamp=datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc),
        starting_capital_krw=Decimal("5000000"),
        ending_capital_krw=Decimal("5000000") + Decimal(realized),
        realized_pnl_krw=Decimal(realized),
        unrealized_pnl_krw=Decimal("0"),
        reconciliation_severity=severity,  # type: ignore[arg-type]
        exception_count=exception_count,
        mode=mode,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# SessionRecord validation
# ---------------------------------------------------------------------------


class TestSessionRecordValidation:
    def test_valid_record_constructs(self):
        rec = _make_record()
        assert rec.session_id == "2026-05-10_120000"
        assert rec.algorithm_version == SCHEMA_VERSION

    def test_empty_session_id_raises(self):
        with pytest.raises(SessionHistoryWriteRecordError, match="session_id"):
            _make_record(session_id="")

    def test_whitespace_session_id_raises(self):
        with pytest.raises(SessionHistoryWriteRecordError, match="session_id"):
            _make_record(session_id="   ")

    def test_naive_timestamp_raises(self):
        with pytest.raises(SessionHistoryWriteRecordError, match="timezone-aware"):
            SessionRecord(
                session_id="s1",
                timestamp=datetime(2026, 5, 10, 12, 0, 0),  # tzinfo 없음
                starting_capital_krw=Decimal("5000000"),
                ending_capital_krw=Decimal("5000000"),
                realized_pnl_krw=Decimal("0"),
                unrealized_pnl_krw=Decimal("0"),
                reconciliation_severity="ok",
                exception_count=0,
                mode="paper",
            )

    def test_non_decimal_field_raises(self):
        with pytest.raises(SessionHistoryWriteRecordError, match="must be Decimal"):
            SessionRecord(
                session_id="s1",
                timestamp=datetime(2026, 5, 10, tzinfo=timezone.utc),
                starting_capital_krw=5000000,  # type: ignore[arg-type]
                ending_capital_krw=Decimal("5000000"),
                realized_pnl_krw=Decimal("0"),
                unrealized_pnl_krw=Decimal("0"),
                reconciliation_severity="ok",
                exception_count=0,
                mode="paper",
            )

    def test_zero_starting_capital_raises(self):
        with pytest.raises(SessionHistoryWriteRecordError, match="starting_capital_krw"):
            SessionRecord(
                session_id="s1",
                timestamp=datetime(2026, 5, 10, tzinfo=timezone.utc),
                starting_capital_krw=Decimal("0"),
                ending_capital_krw=Decimal("0"),
                realized_pnl_krw=Decimal("0"),
                unrealized_pnl_krw=Decimal("0"),
                reconciliation_severity="ok",
                exception_count=0,
                mode="paper",
            )

    def test_invalid_severity_raises(self):
        with pytest.raises(SessionHistoryWriteRecordError, match="severity"):
            _make_record(severity="critical")

    def test_invalid_mode_raises(self):
        with pytest.raises(SessionHistoryWriteRecordError, match="mode"):
            _make_record(mode="backtest")

    def test_negative_exception_count_raises(self):
        with pytest.raises(SessionHistoryWriteRecordError, match="exception_count"):
            _make_record(exception_count=-1)


# ---------------------------------------------------------------------------
# JSON serialization
# ---------------------------------------------------------------------------


class TestJsonSerialization:
    def test_to_jsonl_line_is_single_line_with_newline(self):
        rec = _make_record()
        line = rec.to_jsonl_line()
        assert line.endswith("\n")
        assert line.count("\n") == 1

    def test_decimal_precision_preserved(self):
        rec = SessionRecord(
            session_id="s1",
            timestamp=datetime(2026, 5, 10, tzinfo=timezone.utc),
            starting_capital_krw=Decimal("5000000.123456789"),
            ending_capital_krw=Decimal("5000123.456789"),
            realized_pnl_krw=Decimal("123.456789"),
            unrealized_pnl_krw=Decimal("0"),
            reconciliation_severity="ok",
            exception_count=0,
            mode="paper",
        )
        line = rec.to_jsonl_line()
        parsed = json.loads(line)
        assert parsed["realized_pnl_krw"] == "123.456789"
        assert parsed["starting_capital_krw"] == "5000000.123456789"

    def test_record_too_large_raises(self):
        # session_id 를 매우 크게 만들어 PIPE_BUF 초과 유도
        huge_id = "x" * (MAX_RECORD_LINE_BYTES + 100)
        rec = _make_record(session_id=huge_id)
        with pytest.raises(SessionHistoryWriteRecordError, match="too large"):
            rec.to_jsonl_line()

    def test_jsonl_includes_algorithm_version(self):
        rec = _make_record()
        parsed = json.loads(rec.to_jsonl_line())
        assert parsed["algorithm_version"] == SCHEMA_VERSION


# ---------------------------------------------------------------------------
# File creation & permissions
# ---------------------------------------------------------------------------


class TestFileCreation:
    def test_missing_file_created_with_0600(self, tmp_path):
        f = tmp_path / "sessions.jsonl"
        rec = _make_record()
        append_session_record(f, rec)
        assert f.exists()
        mode = f.stat().st_mode & 0o777
        assert mode == 0o600

    def test_parent_directory_auto_created(self, tmp_path):
        f = tmp_path / "deep" / "nested" / "sessions.jsonl"
        rec = _make_record()
        append_session_record(f, rec)
        assert f.exists()
        assert (tmp_path / "deep" / "nested").is_dir()

    def test_existing_0600_accepts_append(self, tmp_path):
        f = tmp_path / "sessions.jsonl"
        f.touch()
        os.chmod(f, 0o600)
        rec = _make_record()
        append_session_record(f, rec)
        # 라인 수 = 1
        assert f.read_text().count("\n") == 1

    def test_existing_0644_raises(self, tmp_path):
        f = tmp_path / "sessions.jsonl"
        f.touch()
        os.chmod(f, 0o644)
        rec = _make_record()
        with pytest.raises(SessionHistoryWritePermissionError, match="0600"):
            append_session_record(f, rec)

    def test_enforce_permissions_false_skips_check(self, tmp_path):
        f = tmp_path / "sessions.jsonl"
        f.touch()
        os.chmod(f, 0o644)
        rec = _make_record()
        append_session_record(f, rec, enforce_permissions=False)
        # 0644 그대로지만 append 정상
        assert f.read_text().count("\n") == 1


# ---------------------------------------------------------------------------
# Atomic append
# ---------------------------------------------------------------------------


class TestAtomicAppend:
    def test_two_records_produce_two_valid_jsonl_lines(self, tmp_path):
        f = tmp_path / "sessions.jsonl"
        rec1 = _make_record(session_id="s1", realized="100")
        rec2 = _make_record(session_id="s2", realized="-200")
        append_session_record(f, rec1)
        append_session_record(f, rec2)
        lines = f.read_text().strip().split("\n")
        assert len(lines) == 2
        parsed1 = json.loads(lines[0])
        parsed2 = json.loads(lines[1])
        assert parsed1["session_id"] == "s1"
        assert parsed2["session_id"] == "s2"
        assert parsed2["realized_pnl_krw"] == "-200"

    def test_record_round_trip_via_json_load(self, tmp_path):
        f = tmp_path / "sessions.jsonl"
        rec = _make_record(realized="12345")
        append_session_record(f, rec)
        line = f.read_text().strip()
        parsed = json.loads(line)
        assert parsed["session_id"] == rec.session_id
        assert parsed["realized_pnl_krw"] == "12345"
        assert parsed["mode"] == "paper"
        assert parsed["reconciliation_severity"] == "ok"


# ---------------------------------------------------------------------------
# Helper builders
# ---------------------------------------------------------------------------


class TestHelperBuilders:
    def test_build_paper_record_defaults(self):
        rec = build_paper_session_record(
            starting_capital_krw=Decimal("5000000"),
            ending_capital_krw=Decimal("5010000"),
            realized_pnl_krw=Decimal("10000"),
        )
        assert rec.mode == "paper"
        assert rec.unrealized_pnl_krw == Decimal("0")
        assert rec.reconciliation_severity == "missing"
        assert rec.exception_count == 0
        # session_id auto-generated as ISO date_time
        assert re.match(r"\d{4}-\d{2}-\d{2}_\d{6}", rec.session_id)

    def test_build_live_record_mode(self):
        rec = build_live_session_record(
            starting_capital_krw=Decimal("5000000"),
            ending_capital_krw=Decimal("5010000"),
            realized_pnl_krw=Decimal("10000"),
        )
        assert rec.mode == "live"

    def test_build_record_explicit_session_id(self):
        rec = build_paper_session_record(
            starting_capital_krw=Decimal("5000000"),
            ending_capital_krw=Decimal("5000000"),
            realized_pnl_krw=Decimal("0"),
            session_id="custom-id-123",
        )
        assert rec.session_id == "custom-id-123"

    def test_build_record_explicit_timestamp(self):
        ts = datetime(2026, 5, 10, 15, 30, 0, tzinfo=timezone.utc)
        rec = build_paper_session_record(
            starting_capital_krw=Decimal("5000000"),
            ending_capital_krw=Decimal("5000000"),
            realized_pnl_krw=Decimal("0"),
            timestamp=ts,
        )
        assert rec.timestamp == ts


# ---------------------------------------------------------------------------
# try_append_session_record (best-effort wrapper)
# ---------------------------------------------------------------------------


class TestBestEffortWrapper:
    def test_success_returns_true(self, tmp_path):
        f = tmp_path / "sessions.jsonl"
        rec = _make_record()
        ok = try_append_session_record(f, rec)
        assert ok is True
        assert f.exists()

    def test_permission_failure_returns_false(self, tmp_path):
        f = tmp_path / "sessions.jsonl"
        f.touch()
        os.chmod(f, 0o644)
        rec = _make_record()
        ok = try_append_session_record(f, rec)
        assert ok is False
        # 파일 그대로, append 안 됨
        assert f.read_text() == ""

    def test_invalid_record_type_returns_false(self, tmp_path):
        f = tmp_path / "sessions.jsonl"
        # SessionRecord 가 아닌 dict 전달
        ok = try_append_session_record(f, {"not": "a record"})  # type: ignore[arg-type]
        assert ok is False
