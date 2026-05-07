"""
스모크 테스트 — audit_writer (Task A2)
=======================================

JCPR Trading System - jcpr-ts-v01
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from src.observability.audit_writer import (  # noqa: E402
    ALLOWED_EVENT_TYPES,
    SEVERITY_CRITICAL,
    SEVERITY_ERROR,
    SEVERITY_INFO,
    AuditWriter,
    configure_default_writer,
    get_default_writer,
    reset_default_writer,
)
from src.observability.trace_context import (  # noqa: E402
    ORIGIN_OPERATOR,
    TraceContext,
)


# ─────────────────────────────────────────────────
# 헬퍼
# ─────────────────────────────────────────────────

def _ctx() -> TraceContext:
    return TraceContext.new(
        origin=ORIGIN_OPERATOR,
        operator_id="test_user",
        session_id="test-session",
        correlation_keys={"symbol": "005930"},
    )


def _read_jsonl(path: Path) -> list[dict]:
    """JSONL 파일 → dict 리스트."""
    if not path.exists():
        return []
    out = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


# ─────────────────────────────────────────────────
# 기본 쓰기
# ─────────────────────────────────────────────────

def test_basic_write(tmp_dir):
    writer = AuditWriter(audit_dir=str(tmp_dir))
    ctx = _ctx()
    ok = writer.write(
        event_type="signal_generated",
        ctx=ctx,
        payload={"strategy": "momentum_v1", "score": 0.85},
    )
    assert ok is True
    # 파일 생성 확인
    files = list(tmp_dir.glob("audit_*.jsonl"))
    assert len(files) == 1
    records = _read_jsonl(files[0])
    assert len(records) == 1
    assert records[0]["event_type"] == "signal_generated"
    assert records[0]["trace"]["trace_id"] == ctx.trace_id
    print("✅ test_basic_write")


def test_multiple_writes(tmp_dir):
    writer = AuditWriter(audit_dir=str(tmp_dir))
    ctx = _ctx()
    for i in range(5):
        writer.write(event_type="signal_generated", ctx=ctx, payload={"i": i})
    files = list(tmp_dir.glob("audit_*.jsonl"))
    records = _read_jsonl(files[0])
    assert len(records) == 5
    print("✅ test_multiple_writes")


def test_invalid_event_type_falls_back_to_other(tmp_dir):
    writer = AuditWriter(audit_dir=str(tmp_dir))
    ctx = _ctx()
    writer.write(event_type="invented_xyz", ctx=ctx, payload={})
    files = list(tmp_dir.glob("audit_*.jsonl"))
    records = _read_jsonl(files[0])
    assert records[0]["event_type"] == "other"
    assert records[0]["payload"]["_original_event_type"] == "invented_xyz"
    print("✅ test_invalid_event_type_falls_back_to_other")


def test_invalid_severity_falls_back_to_info(tmp_dir):
    writer = AuditWriter(audit_dir=str(tmp_dir))
    ctx = _ctx()
    writer.write(
        event_type="signal_generated",
        ctx=ctx,
        payload={},
        severity="UNKNOWN",
    )
    files = list(tmp_dir.glob("audit_*.jsonl"))
    records = _read_jsonl(files[0])
    assert records[0]["severity"] == "info"
    print("✅ test_invalid_severity_falls_back_to_info")


def test_non_traceconext_rejected(tmp_dir):
    writer = AuditWriter(audit_dir=str(tmp_dir), fail_silently=False)
    try:
        writer.write(
            event_type="signal_generated",
            ctx={"fake": "ctx"},  # type: ignore[arg-type]
            payload={},
        )
        assert False
    except TypeError:
        pass
    print("✅ test_non_traceconext_rejected")


# ─────────────────────────────────────────────────
# 시크릿 마스킹
# ─────────────────────────────────────────────────

def test_secret_in_payload_masked(tmp_dir):
    writer = AuditWriter(audit_dir=str(tmp_dir))
    ctx = _ctx()
    writer.write(
        event_type="signal_generated",
        ctx=ctx,
        payload={
            "normal": "ok",
            "api_key": "SECRET123",
            "nested": {"password": "p"},
        },
    )
    files = list(tmp_dir.glob("audit_*.jsonl"))
    records = _read_jsonl(files[0])
    p = records[0]["payload"]
    assert p["normal"] == "ok"
    assert p["api_key"] == "***MASKED***"
    assert p["nested"]["password"] == "***MASKED***"
    print("✅ test_secret_in_payload_masked")


def test_secret_in_list_masked(tmp_dir):
    writer = AuditWriter(audit_dir=str(tmp_dir))
    ctx = _ctx()
    writer.write(
        event_type="signal_generated",
        ctx=ctx,
        payload={"items": [{"api_key": "x"}, {"normal": "y"}]},
    )
    files = list(tmp_dir.glob("audit_*.jsonl"))
    records = _read_jsonl(files[0])
    items = records[0]["payload"]["items"]
    assert items[0]["api_key"] == "***MASKED***"
    assert items[1]["normal"] == "y"
    print("✅ test_secret_in_list_masked")


# ─────────────────────────────────────────────────
# Decimal / datetime 직렬화
# ─────────────────────────────────────────────────

def test_decimal_serializable(tmp_dir):
    writer = AuditWriter(audit_dir=str(tmp_dir))
    ctx = _ctx()
    writer.write(
        event_type="order_intent",
        ctx=ctx,
        payload={"price_krw": Decimal("70000.50"), "qty": Decimal("100")},
    )
    files = list(tmp_dir.glob("audit_*.jsonl"))
    records = _read_jsonl(files[0])
    # 문자열로 직렬화됨
    assert records[0]["payload"]["price_krw"] == "70000.50"
    print("✅ test_decimal_serializable")


def test_datetime_serializable(tmp_dir):
    writer = AuditWriter(audit_dir=str(tmp_dir))
    ctx = _ctx()
    now = datetime.now(timezone.utc)
    writer.write(
        event_type="order_intent",
        ctx=ctx,
        payload={"submitted_at": now},
    )
    files = list(tmp_dir.glob("audit_*.jsonl"))
    records = _read_jsonl(files[0])
    assert "T" in records[0]["payload"]["submitted_at"]  # ISO format
    print("✅ test_datetime_serializable")


# ─────────────────────────────────────────────────
# 회전 (Rotation)
# ─────────────────────────────────────────────────

def test_daily_rotation(tmp_dir):
    """다른 날짜 → 다른 파일."""
    writer = AuditWriter(audit_dir=str(tmp_dir), rotate_daily=True)
    ctx = _ctx()
    # 오늘 + 어제
    today = datetime(2026, 5, 7, tzinfo=timezone.utc)
    yesterday = datetime(2026, 5, 6, tzinfo=timezone.utc)
    writer.write(event_type="signal_generated", ctx=ctx, payload={},
                 timestamp_utc=today)
    writer.write(event_type="signal_generated", ctx=ctx, payload={},
                 timestamp_utc=yesterday)
    files = list(tmp_dir.glob("audit_*.jsonl"))
    assert len(files) == 2
    names = sorted(f.name for f in files)
    assert "audit_20260506.jsonl" in names
    assert "audit_20260507.jsonl" in names
    print("✅ test_daily_rotation")


def test_no_rotation(tmp_dir):
    writer = AuditWriter(audit_dir=str(tmp_dir), rotate_daily=False)
    ctx = _ctx()
    writer.write(event_type="signal_generated", ctx=ctx, payload={})
    writer.write(event_type="signal_generated", ctx=ctx, payload={})
    files = list(tmp_dir.glob("audit*.jsonl"))
    assert len(files) == 1
    assert files[0].name == "audit.jsonl"
    print("✅ test_no_rotation")


# ─────────────────────────────────────────────────
# 크기 제한
# ─────────────────────────────────────────────────

def test_oversized_payload_truncated(tmp_dir):
    writer = AuditWriter(audit_dir=str(tmp_dir), max_payload_bytes=500)
    ctx = _ctx()
    big = {"data": "x" * 1000}
    writer.write(event_type="signal_generated", ctx=ctx, payload=big)
    files = list(tmp_dir.glob("audit_*.jsonl"))
    records = _read_jsonl(files[0])
    assert records[0]["payload"]["_truncated"] is True
    assert records[0]["severity"] == "warning"
    print("✅ test_oversized_payload_truncated")


# ─────────────────────────────────────────────────
# 편의 메서드
# ─────────────────────────────────────────────────

def test_write_signal(tmp_dir):
    writer = AuditWriter(audit_dir=str(tmp_dir))
    ctx = _ctx()
    writer.write_signal(ctx, payload={"x": 1})
    records = _read_jsonl(list(tmp_dir.glob("audit_*.jsonl"))[0])
    assert records[0]["event_type"] == "signal_generated"
    print("✅ test_write_signal")


def test_write_exception(tmp_dir):
    writer = AuditWriter(audit_dir=str(tmp_dir))
    ctx = _ctx()
    try:
        raise ValueError("test error")
    except ValueError as e:
        writer.write_exception(ctx, e, additional={"context": "test"})
    records = _read_jsonl(list(tmp_dir.glob("audit_*.jsonl"))[0])
    assert records[0]["event_type"] == "exception"
    assert records[0]["severity"] == "error"
    assert records[0]["payload"]["exception_type"] == "ValueError"
    assert records[0]["payload"]["context"] == "test"
    print("✅ test_write_exception")


def test_write_mcp_call(tmp_dir):
    writer = AuditWriter(audit_dir=str(tmp_dir))
    ctx = _ctx()
    writer.write_mcp_call(ctx, payload={"tool": "read_positions"})
    records = _read_jsonl(list(tmp_dir.glob("audit_*.jsonl"))[0])
    assert records[0]["event_type"] == "mcp_tool_call"
    print("✅ test_write_mcp_call")


# ─────────────────────────────────────────────────
# 글로벌 작성기
# ─────────────────────────────────────────────────

def test_default_writer(tmp_dir):
    reset_default_writer()
    assert get_default_writer() is None
    w = configure_default_writer(str(tmp_dir))
    assert get_default_writer() is w
    reset_default_writer()
    print("✅ test_default_writer")


# ─────────────────────────────────────────────────
# 실행
# ─────────────────────────────────────────────────

try:
    import pytest

    @pytest.fixture
    def tmp_dir(tmp_path):
        return tmp_path
except ImportError:
    pass


def _run_all() -> int:
    failed = 0
    tests = [
        test_basic_write,
        test_multiple_writes,
        test_invalid_event_type_falls_back_to_other,
        test_invalid_severity_falls_back_to_info,
        test_non_traceconext_rejected,
        test_secret_in_payload_masked,
        test_secret_in_list_masked,
        test_decimal_serializable,
        test_datetime_serializable,
        test_daily_rotation,
        test_no_rotation,
        test_oversized_payload_truncated,
        test_write_signal,
        test_write_exception,
        test_write_mcp_call,
        test_default_writer,
    ]
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        for fn in tests:
            # 각 테스트마다 새 디렉터리
            sub = td_path / fn.__name__
            sub.mkdir()
            try:
                fn(sub)
            except AssertionError as e:
                print(f"❌ {fn.__name__}: {e}")
                failed += 1
            except Exception as e:  # noqa: BLE001
                print(f"💥 {fn.__name__}: {type(e).__name__}: {e}")
                failed += 1
    return failed


if __name__ == "__main__":
    print("─" * 50)
    print("Task A2 v0.1 — audit_writer 스모크 테스트")
    print("─" * 50)
    failed = _run_all()
    print("─" * 50)
    if failed == 0:
        print("✅ 모든 테스트 통과")
        sys.exit(0)
    else:
        print(f"❌ {failed}개 실패")
        sys.exit(1)
