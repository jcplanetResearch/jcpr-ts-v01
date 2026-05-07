"""
스모크 테스트 — audit_indexer (Task A3)
========================================

JCPR Trading System - jcpr-ts-v01
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from src.observability.audit_indexer import AuditIndexer  # noqa: E402
from src.observability.audit_writer import AuditWriter  # noqa: E402
from src.observability.trace_context import (  # noqa: E402
    ORIGIN_AGENT,
    ORIGIN_OPERATOR,
    TraceContext,
)


# ─────────────────────────────────────────────────
# 헬퍼: 테스트 데이터 생성
# ─────────────────────────────────────────────────

def _setup_test_data(audit_dir: Path) -> dict:
    """3개 trace 시나리오 생성."""
    writer = AuditWriter(audit_dir=str(audit_dir))

    # Trace 1: 정상 거래 (signal → risk → order → fill)
    ctx1 = TraceContext.new(
        origin=ORIGIN_OPERATOR,
        operator_id="alice",
        session_id="sess-1",
        correlation_keys={"symbol": "005930", "strategy_id": "momentum_v1"},
    )
    writer.write_signal(ctx1, payload={"score": 0.85})
    risk1 = ctx1.child_span("risk_eval")
    writer.write_risk(risk1, payload={"decision": "approve"})
    order1 = ctx1.child_span("submit")
    writer.write_order(order1, payload={"qty": 10}, submitted=True)
    fill1 = ctx1.child_span("fill")
    writer.write_fill(fill1, payload={"price_krw": "70000"})

    # Trace 2: 거부된 거래
    ctx2 = TraceContext.new(
        origin=ORIGIN_OPERATOR,
        operator_id="bob",
        session_id="sess-1",
        correlation_keys={"symbol": "035420"},
    )
    writer.write_signal(ctx2, payload={"score": 0.5})
    risk2 = ctx2.child_span("risk_eval")
    writer.write_risk(risk2, payload={"decision": "reject", "reason": "size"})

    # Trace 3: 예외 발생 (Agent 발원)
    ctx3 = TraceContext.new(
        origin=ORIGIN_AGENT,
        operator_id="market_agent",
        session_id="sess-2",
    )
    writer.write_mcp_call(ctx3, payload={"tool": "read_positions"})
    try:
        raise RuntimeError("MCP timeout")
    except RuntimeError as e:
        writer.write_exception(ctx3, e)

    return {
        "trace1": ctx1.trace_id,
        "trace2": ctx2.trace_id,
        "trace3": ctx3.trace_id,
    }


# ─────────────────────────────────────────────────
# 기본 검색
# ─────────────────────────────────────────────────

def test_find_by_trace(audit_dir, traces):
    indexer = AuditIndexer(audit_dir=str(audit_dir))
    events = indexer.find_by_trace(traces["trace1"])
    assert len(events) == 4, f"Expected 4 events, got {len(events)}"
    types = [e.event_type for e in events]
    assert "signal_generated" in types
    assert "risk_evaluation" in types
    assert "order_submitted" in types
    assert "fill_received" in types
    print("✅ test_find_by_trace")


def test_find_by_trace_not_found(audit_dir, traces):
    indexer = AuditIndexer(audit_dir=str(audit_dir))
    events = indexer.find_by_trace("trc-20991231-deadbeef")
    assert events == []
    print("✅ test_find_by_trace_not_found")


def test_search_by_session(audit_dir, traces):
    indexer = AuditIndexer(audit_dir=str(audit_dir))
    events = indexer.search(session_id="sess-1")
    # Trace 1 (4) + Trace 2 (2) = 6
    assert len(events) == 6, f"Got {len(events)}"
    print("✅ test_search_by_session")


def test_search_by_origin(audit_dir, traces):
    indexer = AuditIndexer(audit_dir=str(audit_dir))
    agent_events = indexer.search(origin=ORIGIN_AGENT)
    assert all(e.origin == ORIGIN_AGENT for e in agent_events)
    assert len(agent_events) >= 2  # mcp_tool_call + exception
    print("✅ test_search_by_origin")


def test_search_by_event_type(audit_dir, traces):
    indexer = AuditIndexer(audit_dir=str(audit_dir))
    events = indexer.search(event_types={"risk_evaluation"})
    assert all(e.event_type == "risk_evaluation" for e in events)
    assert len(events) == 2
    print("✅ test_search_by_event_type")


def test_search_by_symbol(audit_dir, traces):
    indexer = AuditIndexer(audit_dir=str(audit_dir))
    events = indexer.search(symbol="005930")
    assert len(events) >= 1
    # 모두 trace1 소속
    assert all(e.trace_id == traces["trace1"] for e in events)
    print("✅ test_search_by_symbol")


def test_search_by_strategy(audit_dir, traces):
    indexer = AuditIndexer(audit_dir=str(audit_dir))
    events = indexer.search(strategy_id="momentum_v1")
    assert len(events) >= 1
    print("✅ test_search_by_strategy")


# ─────────────────────────────────────────────────
# 트리 재구성
# ─────────────────────────────────────────────────

def test_build_trace_tree(audit_dir, traces):
    indexer = AuditIndexer(audit_dir=str(audit_dir))
    tree = indexer.build_trace_tree(traces["trace1"])
    assert tree is not None
    # 루트는 signal_generated
    assert tree.event.event_type == "signal_generated"
    # 자식: risk_eval, submit, fill (모두 root의 자식)
    assert len(tree.children) == 3
    # 총 4개 이벤트
    assert tree.total_events() == 4
    print("✅ test_build_trace_tree")


def test_build_trace_tree_not_found(audit_dir, traces):
    indexer = AuditIndexer(audit_dir=str(audit_dir))
    tree = indexer.build_trace_tree("trc-20991231-deadbeef")
    assert tree is None
    print("✅ test_build_trace_tree_not_found")


def test_trace_summary(audit_dir, traces):
    indexer = AuditIndexer(audit_dir=str(audit_dir))
    s = indexer.trace_summary(traces["trace3"])
    assert s is not None
    assert s.event_count == 2
    assert s.has_exceptions is True
    assert s.origin == ORIGIN_AGENT
    print("✅ test_trace_summary")


# ─────────────────────────────────────────────────
# 집계
# ─────────────────────────────────────────────────

def test_list_traces_by_session(audit_dir, traces):
    indexer = AuditIndexer(audit_dir=str(audit_dir))
    summaries = indexer.list_traces(session_id="sess-1")
    assert len(summaries) == 2  # trace1 + trace2
    print("✅ test_list_traces_by_session")


def test_list_traces_only_exceptions(audit_dir, traces):
    indexer = AuditIndexer(audit_dir=str(audit_dir))
    summaries = indexer.list_traces(only_with_exceptions=True)
    assert len(summaries) == 1
    assert summaries[0].trace_id == traces["trace3"]
    print("✅ test_list_traces_only_exceptions")


def test_stats(audit_dir, traces):
    indexer = AuditIndexer(audit_dir=str(audit_dir))
    s = indexer.stats()
    assert s["total_events"] == 8
    assert s["unique_traces"] == 3
    assert s["unique_sessions"] == 2
    assert "signal_generated" in s["by_event_type"]
    assert "operator" in s["by_origin"]
    assert "agent" in s["by_origin"]
    print("✅ test_stats")


# ─────────────────────────────────────────────────
# 파일 처리
# ─────────────────────────────────────────────────

def test_list_files(audit_dir, traces):
    indexer = AuditIndexer(audit_dir=str(audit_dir))
    files = indexer.list_files()
    assert len(files) >= 1
    print("✅ test_list_files")


def test_missing_dir():
    indexer = AuditIndexer(audit_dir="/tmp/nonexistent_xyz_888")
    files = indexer.list_files()
    assert files == []
    events = indexer.find_by_trace("any")
    assert events == []
    print("✅ test_missing_dir")


def test_corrupted_jsonl_skipped(audit_dir, traces):
    """잘못된 JSON 라인은 skip."""
    # 기존 파일에 잘못된 라인 추가
    files = list(audit_dir.glob("audit_*.jsonl"))
    if files:
        with files[0].open("a") as f:
            f.write("not valid json\n")
            f.write("\n")  # 빈 줄
    indexer = AuditIndexer(audit_dir=str(audit_dir))
    # 정상 이벤트는 여전히 로드됨
    events = indexer.find_by_trace(traces["trace1"])
    assert len(events) == 4
    print("✅ test_corrupted_jsonl_skipped")


# ─────────────────────────────────────────────────
# 시간 필터
# ─────────────────────────────────────────────────

def test_search_with_time_filter(audit_dir, traces):
    indexer = AuditIndexer(audit_dir=str(audit_dir))
    # 미래 시각 이후 → 빈 결과
    far_future = datetime.now(timezone.utc) + timedelta(days=365)
    events = indexer.search(since_utc=far_future)
    assert events == []
    # 과거 → 모든 이벤트
    far_past = datetime.now(timezone.utc) - timedelta(days=365)
    events = indexer.search(since_utc=far_past)
    assert len(events) == 8
    print("✅ test_search_with_time_filter")


# ─────────────────────────────────────────────────
# 실행
# ─────────────────────────────────────────────────

try:
    import pytest

    @pytest.fixture
    def audit_dir(tmp_path):
        return tmp_path

    @pytest.fixture
    def traces(audit_dir):
        return _setup_test_data(audit_dir)
except ImportError:
    pass


def _run_all() -> int:
    failed = 0
    with tempfile.TemporaryDirectory() as td:
        audit_dir = Path(td)
        traces = _setup_test_data(audit_dir)

        # 인자 받는 테스트
        param_tests = [
            test_find_by_trace,
            test_find_by_trace_not_found,
            test_search_by_session,
            test_search_by_origin,
            test_search_by_event_type,
            test_search_by_symbol,
            test_search_by_strategy,
            test_build_trace_tree,
            test_build_trace_tree_not_found,
            test_trace_summary,
            test_list_traces_by_session,
            test_list_traces_only_exceptions,
            test_stats,
            test_list_files,
            test_search_with_time_filter,
        ]
        for fn in param_tests:
            try:
                fn(audit_dir, traces)
            except AssertionError as e:
                print(f"❌ {fn.__name__}: {e}")
                failed += 1
            except Exception as e:  # noqa: BLE001
                print(f"💥 {fn.__name__}: {type(e).__name__}: {e}")
                failed += 1

        # 인자 없는 테스트
        try:
            test_missing_dir()
        except AssertionError as e:
            print(f"❌ test_missing_dir: {e}")
            failed += 1
        except Exception as e:  # noqa: BLE001
            print(f"💥 test_missing_dir: {type(e).__name__}: {e}")
            failed += 1

        # 손상 라인 (별도 디렉터리)
        with tempfile.TemporaryDirectory() as td2:
            audit_dir2 = Path(td2)
            traces2 = _setup_test_data(audit_dir2)
            try:
                test_corrupted_jsonl_skipped(audit_dir2, traces2)
            except AssertionError as e:
                print(f"❌ test_corrupted_jsonl_skipped: {e}")
                failed += 1
            except Exception as e:  # noqa: BLE001
                print(f"💥 test_corrupted_jsonl_skipped: {type(e).__name__}: {e}")
                failed += 1

    return failed


if __name__ == "__main__":
    print("─" * 50)
    print("Task A3 v0.1 — audit_indexer 스모크 테스트")
    print("─" * 50)
    failed = _run_all()
    print("─" * 50)
    if failed == 0:
        print("✅ 모든 테스트 통과")
        sys.exit(0)
    else:
        print(f"❌ {failed}개 실패")
        sys.exit(1)
