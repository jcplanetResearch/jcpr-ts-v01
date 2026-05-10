"""Session history reader (Phase 2 A2-1).

다음 세션 capacity 권장에 N일 P&L history를 활용하기 위한 read-only 모듈.

현 사이클(A2-1)에서는 session jsonl 저장 모듈이 아직 도입되지 않았으므로:
    - data/audit/sessions.jsonl 미존재 → None 반환 (정상 fallback)
    - 비어있는 파일 → None 반환 (정상 fallback)
    - 형식 오류 줄 → 해당 줄만 skip + 카운트 (전체 fail-open)

다음 사이클(A2-2)에서 paper_runner / live_runner 종료 시
이 jsonl에 daily snapshot을 append하면 자동 활성화된다(코드 변경 없이).

보안 영향 (Security Impact):
    - 0600 권한 강제 (layer 17, assert_audit_logs_secured)
    - 사용자 시크릿 / 자격증명 미접촉 (P&L 숫자만 처리)
    - read-only — 절대 jsonl 쓰지 않음

JSONL Schema (A2-2에서 writer가 따라야 할 형식):
    {
        "session_id": "2026-05-10",
        "timestamp": "2026-05-10T15:30:00+09:00",
        "starting_capital_krw": "5000000",
        "ending_capital_krw": "5012500",
        "realized_pnl_krw": "12500",
        "unrealized_pnl_krw": "0",
        "reconciliation_severity": "ok",
        "exception_count": 0,
        "mode": "paper" | "live"
    }
"""
from __future__ import annotations

import json
import logging
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public types — capacity_advisor.HistoryStats와 동일 시그니처여야 함
# ---------------------------------------------------------------------------
# (HistoryStats는 src/risk/capacity_advisor.py 에서 import 하지 않고 dict로 반환,
#  bridge 계층에서 변환 — 모듈 간 결합도 최소화)


@dataclass(frozen=True)
class _SessionRecord:
    """jsonl 한 줄을 표현하는 내부 dataclass."""

    session_id: str
    timestamp: datetime
    realized_pnl_krw: Decimal
    starting_capital_krw: Decimal


@dataclass(frozen=True)
class HistoryReadResult:
    """history reader 산출물.

    Attributes:
        sessions_count: 유효하게 파싱된 세션 수
        cumulative_realized_pnl_krw: 누적 실현 P&L
        max_drawdown_krw: 최대 낙폭 (양의 magnitude, 0 이상)
        consecutive_loss_days: 직전 연속 손실 일수
        skipped_lines: 형식 오류로 skip된 줄 수
        source_path: 읽은 jsonl 경로
    """

    sessions_count: int
    cumulative_realized_pnl_krw: Decimal
    max_drawdown_krw: Decimal
    consecutive_loss_days: int
    skipped_lines: int
    source_path: Path


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SessionHistoryError(Exception):
    """session history reader 베이스 예외."""


class SessionHistoryPermissionError(SessionHistoryError):
    """jsonl 파일 권한이 0600 이 아님."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def try_load_history(
    audit_path: Path,
    days: int = 30,
    *,
    enforce_permissions: bool = True,
) -> HistoryReadResult | None:
    """data/audit/sessions.jsonl 에서 최근 N일 세션 통계 읽기.

    Args:
        audit_path: jsonl 파일 경로 (data/audit/sessions.jsonl 등).
        days: 최근 N일 (default 30). 1 이상.
        enforce_permissions: True 면 0600 권한 강제 (layer 17).

    Returns:
        HistoryReadResult 또는 None (파일 부재 / 비어있음 / 모든 줄 형식 오류).

    Raises:
        SessionHistoryPermissionError: enforce_permissions=True + 권한 위반.
        SessionHistoryError: 그 외 치명적 오류 (잘못된 days 등).
    """
    if days < 1:
        raise SessionHistoryError(f"days must be >= 1, got {days}")

    # 파일 부재 → None (정상 fallback)
    if not audit_path.exists():
        logger.debug(
            "session history jsonl 미존재 — fallback to None (path=%s)", audit_path
        )
        return None

    # 권한 검증 (layer 17)
    if enforce_permissions:
        _assert_0600(audit_path)

    # 파일 크기 0 → None
    try:
        if audit_path.stat().st_size == 0:
            return None
    except OSError as exc:
        logger.warning("session history stat 실패: %s", exc)
        return None

    # 줄 단위 파싱
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    records: list[_SessionRecord] = []
    skipped = 0

    try:
        with audit_path.open("r", encoding="utf-8") as f:
            for lineno, raw_line in enumerate(f, start=1):
                line = raw_line.strip()
                if not line:
                    continue
                rec = _parse_line(line, lineno)
                if rec is None:
                    skipped += 1
                    continue
                if rec.timestamp < cutoff:
                    continue
                records.append(rec)
    except OSError as exc:
        logger.warning("session history 읽기 실패: %s", exc)
        return None

    if not records:
        return None

    # 정렬 (timestamp asc)
    records.sort(key=lambda r: r.timestamp)

    # 통계 집계
    cumulative_pnl = sum(
        (r.realized_pnl_krw for r in records), start=Decimal("0")
    )
    max_dd = _compute_max_drawdown(records)
    consec_loss = _count_consecutive_losses(records)

    return HistoryReadResult(
        sessions_count=len(records),
        cumulative_realized_pnl_krw=cumulative_pnl,
        max_drawdown_krw=max_dd,
        consecutive_loss_days=consec_loss,
        skipped_lines=skipped,
        source_path=audit_path,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _assert_0600(path: Path) -> None:
    """POSIX 권한 0600 강제 (layer 17)."""
    try:
        mode = path.stat().st_mode & 0o777
    except OSError as exc:
        raise SessionHistoryPermissionError(
            f"권한 확인 실패: {path}: {exc}"
        ) from exc
    if mode != 0o600:
        raise SessionHistoryPermissionError(
            f"session history jsonl 권한이 0600 이 아님 "
            f"(path={path}, actual={oct(mode)}). "
            f"chmod 600 으로 수정 후 재시도하십시오."
        )


def _parse_line(line: str, lineno: int) -> _SessionRecord | None:
    """jsonl 한 줄을 파싱. 형식 오류 시 None 반환 + 경고 로그."""
    try:
        data: dict[str, Any] = json.loads(line)
    except json.JSONDecodeError as exc:
        logger.warning("session history line %d JSON 파싱 실패: %s", lineno, exc)
        return None

    if not isinstance(data, dict):
        logger.warning(
            "session history line %d 객체가 아님 (got %s)", lineno, type(data).__name__
        )
        return None

    try:
        session_id = str(data["session_id"])
        ts_raw = data["timestamp"]
        realized_raw = data["realized_pnl_krw"]
        starting_raw = data["starting_capital_krw"]
    except KeyError as exc:
        logger.warning(
            "session history line %d 필수 키 누락: %s", lineno, exc
        )
        return None

    # timestamp 파싱
    try:
        if isinstance(ts_raw, str):
            ts = datetime.fromisoformat(ts_raw)
        else:
            logger.warning(
                "session history line %d timestamp가 문자열이 아님", lineno
            )
            return None
        if ts.tzinfo is None:
            # naive → UTC 가정
            ts = ts.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError) as exc:
        logger.warning(
            "session history line %d timestamp 파싱 실패: %s", lineno, exc
        )
        return None

    # Decimal 파싱
    try:
        realized = Decimal(str(realized_raw))
        starting = Decimal(str(starting_raw))
    except (InvalidOperation, ValueError) as exc:
        logger.warning(
            "session history line %d 금액 파싱 실패: %s", lineno, exc
        )
        return None

    if starting <= 0:
        logger.warning(
            "session history line %d starting_capital_krw가 양수가 아님: %s",
            lineno,
            starting,
        )
        return None

    return _SessionRecord(
        session_id=session_id,
        timestamp=ts,
        realized_pnl_krw=realized,
        starting_capital_krw=starting,
    )


def _compute_max_drawdown(records: list[_SessionRecord]) -> Decimal:
    """누적 실현 P&L 곡선의 최대 낙폭(magnitude, ≥ 0) 계산."""
    if not records:
        return Decimal("0")
    cumulative = Decimal("0")
    peak = Decimal("0")
    max_dd = Decimal("0")
    for r in records:
        cumulative += r.realized_pnl_krw
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd
    return max_dd


def _count_consecutive_losses(records: list[_SessionRecord]) -> int:
    """timestamp 정렬된 records의 직전 연속 손실 일수 계산."""
    if not records:
        return 0
    # 가장 최근부터 역순 — realized_pnl_krw < 0 인 연속 카운트
    count = 0
    for r in reversed(records):
        if r.realized_pnl_krw < 0:
            count += 1
        else:
            break
    return count


__all__ = (
    "HistoryReadResult",
    "SessionHistoryError",
    "SessionHistoryPermissionError",
    "try_load_history",
)
