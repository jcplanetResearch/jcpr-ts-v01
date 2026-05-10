"""Phase 2 A2-1 — dashboard bridge + history reader 통합 테스트.

Test categories:
    - bridge: ladder 미정의 → no_ladder
    - bridge: ladder 정의 + 양의 P&L → up
    - bridge: history jsonl 부재 → graceful (None)
    - bridge: history jsonl 권한 위반 → fail-open + 권장은 history 없이
    - bridge: 후방 호환 — 인자 없이 호출 시 manual fallback
    - bridge: Phase 1 호환 키 (available, reason) 보존
    - history reader: 빈 파일 → None
    - history reader: 형식 오류 줄 → skip + 카운트
    - history reader: 0644 권한 → SessionHistoryPermissionError
"""
from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

# capacity_advisor (테스트 대상이 import 하는 실제 모듈)
from src.risk.capacity_advisor import CapacityAdvisor

# Phase 2 A2-1 머지 후 정상 import 경로 — 사용자 로컬 _phase1_bridge.py 에
# get_capacity_recommendation_status 가 머지되어 있어야 함.
# (이전 fix 전: from src.dashboard.views._phase1_bridge_patch import ...)
from src.dashboard.views._phase1_bridge import (
    get_capacity_recommendation_status,
)

from src.dashboard.views._session_history_reader import (
    SessionHistoryPermissionError,
    try_load_history,
)


# ---------------------------------------------------------------------------
# Fake CapacityConfig (사용자 로컬 _config.py 의 CapacityConfig 모방)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FakeCapacityConfig:
    """테스트용 — CapacityConfig.ladder + starting_capital_krw 만 보유."""

    starting_capital_krw: Decimal
    ladder: tuple[Decimal, ...] = field(default_factory=tuple)
    ladder_unit: str = "KRW"


def _make_config(
    starting: str = "5000000",
    ladder: tuple[str, ...] = (
        "1000000",
        "2000000",
        "5000000",
        "10000000",
        "20000000",
    ),
) -> FakeCapacityConfig:
    return FakeCapacityConfig(
        starting_capital_krw=Decimal(starting),
        ladder=tuple(Decimal(v) for v in ladder),
    )


# ---------------------------------------------------------------------------
# Bridge tests
# ---------------------------------------------------------------------------


class TestBridge:
    def test_no_args_returns_manual_fallback(self):
        """후방 호환 — 인자 없이 호출 시 manual."""
        result = get_capacity_recommendation_status()
        assert result["available"] is False
        assert result["reason"] == "manual"
        assert result["recommendation"] is None

    def test_no_ladder_returns_no_ladder(self):
        config = _make_config(ladder=())
        result = get_capacity_recommendation_status(
            config=config,
            session_summary={"realized_pnl_krw": "100", "unrealized_pnl_krw": "0"},
            reconciliation={"available": True, "severity": "ok"},
            audit_summary={"exec_failed_count": 0},
        )
        assert result["available"] is False
        assert result["reason"] == "no_ladder"
        assert result["recommendation"] is None

    def test_positive_pnl_yields_up_recommendation(self):
        config = _make_config()
        result = get_capacity_recommendation_status(
            config=config,
            session_summary={
                "realized_pnl_krw": "250000",
                "unrealized_pnl_krw": "0",
            },
            reconciliation={"available": True, "severity": "ok"},
            audit_summary={"exec_failed_count": 0},
        )
        assert result["available"] is True
        assert result["reason"] == "computed_v1"
        rec = result["recommendation"]
        assert rec is not None
        assert rec["direction"] == "up"
        assert rec["ladder_step_from"] == 2
        assert rec["ladder_step_to"] == 3

    def test_recon_none_treated_as_missing(self):
        config = _make_config()
        # reconciliation = None → severity "missing" → 상승 무효화
        result = get_capacity_recommendation_status(
            config=config,
            session_summary={
                "realized_pnl_krw": "250000",
                "unrealized_pnl_krw": "0",
            },
            reconciliation=None,
            audit_summary={"exec_failed_count": 0},
        )
        assert result["available"] is True
        rec = result["recommendation"]
        assert rec["direction"] == "hold"

    def test_recon_unavailable_treated_as_missing(self):
        config = _make_config()
        result = get_capacity_recommendation_status(
            config=config,
            session_summary={
                "realized_pnl_krw": "-250000",
                "unrealized_pnl_krw": "0",
            },
            reconciliation={"available": False, "reason": "missing_jsonl"},
            audit_summary={"exec_failed_count": 0},
        )
        assert result["available"] is True
        rec = result["recommendation"]
        assert rec["direction"] == "down"  # 음의 P&L 은 missing 무관하게 down

    def test_history_path_missing_graceful(self, tmp_path):
        config = _make_config()
        missing = tmp_path / "does_not_exist.jsonl"
        result = get_capacity_recommendation_status(
            config=config,
            session_summary={
                "realized_pnl_krw": "0",
                "unrealized_pnl_krw": "0",
            },
            reconciliation={"available": True, "severity": "ok"},
            audit_summary={"exec_failed_count": 0},
            history_path=missing,
        )
        # history 부재 → 권장은 단일 세션 모드로 작동
        assert result["available"] is True
        rec = result["recommendation"]
        assert rec["direction"] == "hold"
        # history 관련 trigger 가 없거나 None 이어야 함
        assert "history_sessions_count" not in rec["triggers"]

    def test_history_permission_error_fails_open(self, tmp_path):
        """history jsonl 권한 위반 → 권장은 history 없이 진행 (fail-open)."""
        config = _make_config()
        bad_perm = tmp_path / "sessions.jsonl"
        bad_perm.write_text(
            json.dumps({
                "session_id": "2026-05-09",
                "timestamp": "2026-05-09T15:30:00+00:00",
                "realized_pnl_krw": "-100",
                "starting_capital_krw": "5000000",
            })
            + "\n"
        )
        os.chmod(bad_perm, 0o644)  # 잘못된 권한

        result = get_capacity_recommendation_status(
            config=config,
            session_summary={
                "realized_pnl_krw": "250000",
                "unrealized_pnl_krw": "0",
            },
            reconciliation={"available": True, "severity": "ok"},
            audit_summary={"exec_failed_count": 0},
            history_path=bad_perm,
            enforce_history_permissions=True,
        )
        # fail-open: 권장 자체는 정상 산출 (history 없이)
        assert result["available"] is True
        rec = result["recommendation"]
        assert "history_sessions_count" not in rec["triggers"]

    def test_phase1_compatibility_keys_preserved(self):
        """기존 호출자가 'available', 'reason' 만 읽어도 안전."""
        config = _make_config()
        result = get_capacity_recommendation_status(
            config=config,
            session_summary={"realized_pnl_krw": "0", "unrealized_pnl_krw": "0"},
            reconciliation={"available": True, "severity": "ok"},
            audit_summary={"exec_failed_count": 0},
        )
        # Phase 1 호출자가 의존하는 키
        assert "available" in result
        assert "reason" in result
        # Phase 2 신규 키
        assert "recommendation" in result


# ---------------------------------------------------------------------------
# History reader tests
# ---------------------------------------------------------------------------


class TestHistoryReader:
    def test_missing_file_returns_none(self, tmp_path):
        result = try_load_history(tmp_path / "missing.jsonl")
        assert result is None

    def test_empty_file_returns_none(self, tmp_path):
        empty = tmp_path / "empty.jsonl"
        empty.touch()
        os.chmod(empty, 0o600)
        result = try_load_history(empty)
        assert result is None

    def test_wrong_permission_raises(self, tmp_path):
        f = tmp_path / "sessions.jsonl"
        f.write_text(
            json.dumps({
                "session_id": "s1",
                "timestamp": "2026-05-09T00:00:00+00:00",
                "realized_pnl_krw": "100",
                "starting_capital_krw": "5000000",
            })
            + "\n"
        )
        os.chmod(f, 0o644)
        with pytest.raises(SessionHistoryPermissionError, match="0600"):
            try_load_history(f, enforce_permissions=True)

    def test_malformed_lines_skipped(self, tmp_path):
        f = tmp_path / "sessions.jsonl"
        # 추후 chmod 600 해야 read 가능
        lines = [
            json.dumps({
                "session_id": "s1",
                "timestamp": "2026-05-09T00:00:00+00:00",
                "realized_pnl_krw": "100",
                "starting_capital_krw": "5000000",
            }),
            "not-json-at-all",
            json.dumps({"missing": "fields"}),
            json.dumps({
                "session_id": "s2",
                "timestamp": "2026-05-10T00:00:00+00:00",
                "realized_pnl_krw": "200",
                "starting_capital_krw": "5000000",
            }),
        ]
        f.write_text("\n".join(lines) + "\n")
        os.chmod(f, 0o600)
        result = try_load_history(f, days=365)
        assert result is not None
        assert result.sessions_count == 2
        assert result.skipped_lines == 2
        assert result.cumulative_realized_pnl_krw == Decimal("300")

    def test_consecutive_losses_counted(self, tmp_path):
        f = tmp_path / "sessions.jsonl"
        lines = [
            # 가장 오래된 → 최근 순
            {"realized": "100", "ts": "2026-05-01T00:00:00+00:00"},
            {"realized": "-50", "ts": "2026-05-02T00:00:00+00:00"},
            {"realized": "-30", "ts": "2026-05-03T00:00:00+00:00"},
            {"realized": "-20", "ts": "2026-05-04T00:00:00+00:00"},
        ]
        content = "\n".join(
            json.dumps({
                "session_id": f"s{i}",
                "timestamp": L["ts"],
                "realized_pnl_krw": L["realized"],
                "starting_capital_krw": "5000000",
            })
            for i, L in enumerate(lines)
        )
        f.write_text(content + "\n")
        os.chmod(f, 0o600)
        result = try_load_history(f, days=365)
        assert result is not None
        assert result.consecutive_loss_days == 3

    def test_max_drawdown_computed(self, tmp_path):
        f = tmp_path / "sessions.jsonl"
        # cumulative 곡선: 0 → 1000 → 2000 → 1500 → 500 → 700
        # peak=2000, trough=500, MDD=1500
        deltas = ["1000", "1000", "-500", "-1000", "200"]
        ts_base = datetime(2026, 5, 1, tzinfo=timezone.utc)
        from datetime import timedelta as TD
        content_lines = []
        for i, d in enumerate(deltas):
            content_lines.append(
                json.dumps({
                    "session_id": f"s{i}",
                    "timestamp": (ts_base + TD(days=i)).isoformat(),
                    "realized_pnl_krw": d,
                    "starting_capital_krw": "5000000",
                })
            )
        f.write_text("\n".join(content_lines) + "\n")
        os.chmod(f, 0o600)
        result = try_load_history(f, days=365)
        assert result is not None
        assert result.max_drawdown_krw == Decimal("1500")
