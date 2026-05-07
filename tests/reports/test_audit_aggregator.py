"""
스모크 테스트 — audit_aggregator
=================================

JCPR Trading System - jcpr-ts-v01
Task 49 v0.2
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from src.reports.audit_aggregator import (  # noqa: E402
    aggregate_approval_audit,
    aggregate_execution_audit,
    aggregate_risk_audit,
)


# ─────────────────────────────────────────────────
# 픽스처
# ─────────────────────────────────────────────────

def _write_jsonl(path: Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _make_risk_records(now: datetime) -> list[dict]:
    return [
        {"evaluated_at_utc": (now - timedelta(minutes=30)).isoformat(),
         "decision": "approve", "symbol": "005930", "strategy_id": "momentum_v1"},
        {"evaluated_at_utc": (now - timedelta(minutes=20)).isoformat(),
         "decision": "reject", "rejected_gate": "position_size_limit",
         "rejection_reason": "exceeds_max_position", "symbol": "035420",
         "strategy_id": "momentum_v1"},
        {"evaluated_at_utc": (now - timedelta(minutes=10)).isoformat(),
         "decision": "reject", "rejected_gate": "daily_loss_limit",
         "rejection_reason": "daily_loss_exceeded", "symbol": "005930",
         "strategy_id": "mean_rev"},
        {"evaluated_at_utc": (now - timedelta(minutes=5)).isoformat(),
         "decision": "approve", "symbol": "005930", "strategy_id": "momentum_v1"},
    ]


def _make_exec_records(now: datetime) -> list[dict]:
    return [
        {"started_at_utc": (now - timedelta(minutes=25)).isoformat(),
         "execution_id": "e1", "outcome": "success", "symbol": "005930"},
        {"started_at_utc": (now - timedelta(minutes=15)).isoformat(),
         "execution_id": "e2", "outcome": "error", "stage": "submit",
         "error": "broker timeout", "symbol": "035420"},
        {"started_at_utc": (now - timedelta(minutes=5)).isoformat(),
         "execution_id": "e3", "outcome": "cancelled", "symbol": "005930"},
    ]


def _make_approval_records(now: datetime) -> list[dict]:
    return [
        {"requested_at_utc": (now - timedelta(minutes=20)).isoformat(),
         "outcome": "approved", "auto_approved": False},
        {"requested_at_utc": (now - timedelta(minutes=15)).isoformat(),
         "outcome": "approved", "auto_approved": True},
        {"requested_at_utc": (now - timedelta(minutes=10)).isoformat(),
         "outcome": "declined"},
        {"requested_at_utc": (now - timedelta(minutes=5)).isoformat(),
         "outcome": "timeout"},
    ]


# ─────────────────────────────────────────────────
# 테스트
# ─────────────────────────────────────────────────

def test_risk_audit_empty_path():
    s = aggregate_risk_audit(None)
    assert s.total_evaluations == 0
    assert s.rejection_rate == 0.0
    print("✅ test_risk_audit_empty_path")


def test_risk_audit_missing_file():
    s = aggregate_risk_audit("/tmp/nonexistent_xyz.jsonl")
    assert s.total_evaluations == 0
    print("✅ test_risk_audit_missing_file")


def test_risk_audit_full(tmp_dir, now):
    p = tmp_dir / "risk.jsonl"
    _write_jsonl(p, _make_risk_records(now))
    s = aggregate_risk_audit(str(p))
    assert s.total_evaluations == 4
    assert s.approved == 2
    assert s.rejected == 2
    assert s.rejection_rate == 0.5
    assert "position_size_limit" in s.by_gate
    assert "daily_loss_limit" in s.by_gate
    assert s.by_symbol_rejected.get("035420") == 1
    assert s.by_symbol_rejected.get("005930") == 1
    assert len(s.sample_rejections) == 2
    print("✅ test_risk_audit_full")


def test_risk_audit_blank_lines(tmp_dir, now):
    """빈 줄 + 잘못된 JSON skip 확인."""
    p = tmp_dir / "risk_blank.jsonl"
    with p.open("w") as f:
        f.write("\n")
        f.write(json.dumps({"evaluated_at_utc": now.isoformat(),
                            "decision": "approve"}) + "\n")
        f.write("not valid json\n")
        f.write("\n")
        f.write(json.dumps({"evaluated_at_utc": now.isoformat(),
                            "decision": "reject",
                            "rejected_gate": "g1"}) + "\n")
    s = aggregate_risk_audit(str(p))
    assert s.total_evaluations == 2, f"Got {s.total_evaluations}"
    assert s.rejected == 1
    print("✅ test_risk_audit_blank_lines")


def test_risk_audit_time_filter(tmp_dir, now):
    """time filter — 범위 외 레코드 제외."""
    p = tmp_dir / "risk_filter.jsonl"
    _write_jsonl(p, _make_risk_records(now))
    # 범위: now-15min ~ now → 마지막 2건만
    s = aggregate_risk_audit(
        str(p),
        session_start_utc=now - timedelta(minutes=15),
        session_end_utc=now,
    )
    assert s.total_evaluations == 2, f"Got {s.total_evaluations}"
    print("✅ test_risk_audit_time_filter")


def test_execution_audit_full(tmp_dir, now):
    p = tmp_dir / "exec.jsonl"
    _write_jsonl(p, _make_exec_records(now))
    s = aggregate_execution_audit(str(p))
    assert s.total_executions == 3
    assert s.success == 1
    assert s.error == 1
    assert s.cancelled == 1
    assert s.error_rate == 1 / 3
    assert "submit" in s.by_stage
    assert len(s.error_messages) == 1
    em = s.error_messages[0]
    assert em["execution_id"] == "e2"
    assert "broker timeout" in em["message"]
    print("✅ test_execution_audit_full")


def test_approval_audit_full(tmp_dir, now):
    p = tmp_dir / "appr.jsonl"
    _write_jsonl(p, _make_approval_records(now))
    s = aggregate_approval_audit(str(p))
    assert s.total_requests == 4
    assert s.approved == 2
    assert s.auto_approved == 1
    assert s.declined == 1
    assert s.timeout == 1
    assert s.approval_rate == 0.5
    print("✅ test_approval_audit_full")


def test_secret_filtering(tmp_dir, now):
    """sample_rejections에서 시크릿성 키 제거 확인."""
    p = tmp_dir / "risk_secret.jsonl"
    _write_jsonl(p, [
        {"evaluated_at_utc": now.isoformat(), "decision": "reject",
         "rejected_gate": "g1", "symbol": "A",
         "api_key": "SECRETKEY12345", "auth_token": "TOKEN", "normal_field": "ok"},
    ])
    s = aggregate_risk_audit(str(p))
    assert len(s.sample_rejections) == 1
    sample = s.sample_rejections[0]
    assert "api_key" not in sample, "api_key should be filtered"
    assert "auth_token" not in sample, "auth_token should be filtered"
    assert sample.get("normal_field") == "ok"
    print("✅ test_secret_filtering")


# ─────────────────────────────────────────────────
# 실행 (pytest + standalone)
# ─────────────────────────────────────────────────

try:
    import pytest

    @pytest.fixture
    def tmp_dir(tmp_path):
        return tmp_path

    @pytest.fixture
    def now():
        return datetime.now(timezone.utc)
except ImportError:
    pass


def _run_standalone() -> int:
    failed = 0
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        now = datetime.now(timezone.utc)

        # 인자 없는 테스트
        for fn in [
            test_risk_audit_empty_path,
            test_risk_audit_missing_file,
        ]:
            try:
                fn()
            except AssertionError as e:
                print(f"❌ {fn.__name__}: {e}")
                failed += 1
            except Exception as e:  # noqa: BLE001
                print(f"💥 {fn.__name__}: {type(e).__name__}: {e}")
                failed += 1

        # 픽스처 필요
        for fn in [
            test_risk_audit_full,
            test_risk_audit_blank_lines,
            test_risk_audit_time_filter,
            test_execution_audit_full,
            test_approval_audit_full,
            test_secret_filtering,
        ]:
            try:
                fn(td_path, now)
            except AssertionError as e:
                print(f"❌ {fn.__name__}: {e}")
                failed += 1
            except Exception as e:  # noqa: BLE001
                print(f"💥 {fn.__name__}: {type(e).__name__}: {e}")
                failed += 1

    return failed


if __name__ == "__main__":
    print("─" * 50)
    print("Task 49 v0.2 — audit_aggregator 스모크 테스트")
    print("─" * 50)
    failed = _run_standalone()
    print("─" * 50)
    if failed == 0:
        print("✅ 모든 테스트 통과")
        sys.exit(0)
    else:
        print(f"❌ {failed}개 실패")
        sys.exit(1)
