"""Session history writer (Phase 2 A2-2).

paper_runner / live_runner 종료 시 daily snapshot 을 jsonl 에 append.
A2-1 의 _session_history_reader.py 가 즉시 활성화되어 capacity_advisor 가
N일 history 를 활용한 권장을 산출.

설계 (Design):
    - O_APPEND atomic append (단일 호스트, 단일 writer 가정)
    - 0600 권한 강제 (layer 17 — assert_audit_logs_secured 와 동일 정책)
    - 한 줄 record < 4000 bytes (PIPE_BUF 안전)
    - finally 블록에서 호출 가능 — best-effort, 실패 시 SessionHistoryWriteError
      (호출자가 fail-soft 로 catch 하여 shutdown 우선 정책 보존)

호환 (Compatibility):
    - reader (_session_history_reader.py) 가 기대하는 4 필드:
      session_id, timestamp, realized_pnl_krw, starting_capital_krw
    - writer 는 9 필드 + algorithm_version 기록 (reader 는 추가 필드 무시)
    - 향후 reader 확장 시 알고리즘 v2 등 forward compatibility 보존

보안 (Security):
    - 시크릿 / 자격증명 / 브로커 토큰 미접촉
    - 기록 내용은 P&L 숫자 + session_id (operator-assigned) + severity
    - 17층 보안 게이트: layer 17 (0600 audit log permission) 강제
"""
from __future__ import annotations

import json
import logging
import os
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)


# Public schema version — reader 가 무시하지만 향후 evolution 추적용
SCHEMA_VERSION: str = "session_v1"

# 한 줄 record 최대 크기 (PIPE_BUF on macOS/Linux = 4096 bytes; 안전 마진)
MAX_RECORD_LINE_BYTES: int = 4000


SeverityLiteral = Literal["ok", "minor", "major", "missing"]
ModeLiteral = Literal["paper", "live"]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SessionHistoryWriteError(Exception):
    """writer 베이스 예외."""


class SessionHistoryWritePermissionError(SessionHistoryWriteError):
    """jsonl 파일 권한이 0600 이 아님 (layer 17)."""


class SessionHistoryWriteRecordError(SessionHistoryWriteError):
    """record 검증 실패 또는 직렬화 실패."""


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionRecord:
    """A2-2 풀 스키마 — 9 필드 + algorithm_version.

    Attributes:
        session_id: ISO date or runner-assigned id (operator-friendly).
        timestamp: tz-aware datetime (UTC 권장).
        starting_capital_krw: 세션 시작 자본.
        ending_capital_krw: 세션 종료 자본 (starting + realized).
        realized_pnl_krw: 실현 손익.
        unrealized_pnl_krw: 미실현 손익 (None 허용).
        reconciliation_severity: ok / minor / major / missing.
        exception_count: EXEC_FAILED 등 비정상 종료 카운트.
        mode: paper / live.
        algorithm_version: writer schema version (reader 는 무시).
    """

    session_id: str
    timestamp: datetime
    starting_capital_krw: Decimal
    ending_capital_krw: Decimal
    realized_pnl_krw: Decimal
    unrealized_pnl_krw: Decimal
    reconciliation_severity: SeverityLiteral
    exception_count: int
    mode: ModeLiteral
    algorithm_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        # session_id
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise SessionHistoryWriteRecordError(
                "session_id must be non-empty string"
            )
        # timestamp tz-aware
        if not isinstance(self.timestamp, datetime):
            raise SessionHistoryWriteRecordError(
                f"timestamp must be datetime, got {type(self.timestamp).__name__}"
            )
        if self.timestamp.tzinfo is None:
            raise SessionHistoryWriteRecordError(
                "timestamp must be timezone-aware (use datetime.now(timezone.utc))"
            )
        # Decimal 필드
        for fld in (
            "starting_capital_krw",
            "ending_capital_krw",
            "realized_pnl_krw",
            "unrealized_pnl_krw",
        ):
            v = getattr(self, fld)
            if not isinstance(v, Decimal):
                raise SessionHistoryWriteRecordError(
                    f"{fld} must be Decimal, got {type(v).__name__}"
                )
        if self.starting_capital_krw <= 0:
            raise SessionHistoryWriteRecordError(
                f"starting_capital_krw must be positive, got {self.starting_capital_krw}"
            )
        # severity
        if self.reconciliation_severity not in ("ok", "minor", "major", "missing"):
            raise SessionHistoryWriteRecordError(
                f"invalid severity: {self.reconciliation_severity!r}"
            )
        # exception_count
        if not isinstance(self.exception_count, int) or self.exception_count < 0:
            raise SessionHistoryWriteRecordError(
                f"exception_count must be non-negative int, got {self.exception_count!r}"
            )
        # mode
        if self.mode not in ("paper", "live"):
            raise SessionHistoryWriteRecordError(
                f"invalid mode: {self.mode!r} (expected 'paper' or 'live')"
            )
        # algorithm_version
        if not isinstance(self.algorithm_version, str) or not self.algorithm_version:
            raise SessionHistoryWriteRecordError(
                "algorithm_version must be non-empty string"
            )

    def to_jsonl_line(self) -> str:
        """JSON serialize + trailing newline. Decimal → str, datetime → isoformat."""
        payload: dict[str, Any] = {
            "session_id": self.session_id,
            "timestamp": self.timestamp.isoformat(),
            "starting_capital_krw": str(self.starting_capital_krw),
            "ending_capital_krw": str(self.ending_capital_krw),
            "realized_pnl_krw": str(self.realized_pnl_krw),
            "unrealized_pnl_krw": str(self.unrealized_pnl_krw),
            "reconciliation_severity": self.reconciliation_severity,
            "exception_count": self.exception_count,
            "mode": self.mode,
            "algorithm_version": self.algorithm_version,
        }
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        encoded_len = len(line.encode("utf-8"))
        if encoded_len > MAX_RECORD_LINE_BYTES:
            raise SessionHistoryWriteRecordError(
                f"record line too large: {encoded_len} bytes "
                f"(max {MAX_RECORD_LINE_BYTES})"
            )
        return line


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def build_paper_session_record(
    *,
    starting_capital_krw: Decimal,
    ending_capital_krw: Decimal,
    realized_pnl_krw: Decimal,
    unrealized_pnl_krw: Decimal | None = None,
    reconciliation_severity: SeverityLiteral = "missing",
    exception_count: int = 0,
    session_id: str | None = None,
    timestamp: datetime | None = None,
) -> SessionRecord:
    """편의 헬퍼 — paper 모드 + 누락 필드 default.

    Default 정책:
        - session_id None → ISO 'YYYY-MM-DD_HHMMSS' (UTC) 자동 생성
        - timestamp None → datetime.now(timezone.utc)
        - unrealized_pnl_krw None → Decimal('0')
    """
    now = timestamp if timestamp is not None else datetime.now(timezone.utc)
    if session_id is None:
        session_id = now.strftime("%Y-%m-%d_%H%M%S")
    if unrealized_pnl_krw is None:
        unrealized_pnl_krw = Decimal("0")

    return SessionRecord(
        session_id=session_id,
        timestamp=now,
        starting_capital_krw=starting_capital_krw,
        ending_capital_krw=ending_capital_krw,
        realized_pnl_krw=realized_pnl_krw,
        unrealized_pnl_krw=unrealized_pnl_krw,
        reconciliation_severity=reconciliation_severity,
        exception_count=exception_count,
        mode="paper",
    )


def build_live_session_record(
    *,
    starting_capital_krw: Decimal,
    ending_capital_krw: Decimal,
    realized_pnl_krw: Decimal,
    unrealized_pnl_krw: Decimal | None = None,
    reconciliation_severity: SeverityLiteral = "missing",
    exception_count: int = 0,
    session_id: str | None = None,
    timestamp: datetime | None = None,
) -> SessionRecord:
    """편의 헬퍼 — live 모드 (T42 시점 활성화)."""
    now = timestamp if timestamp is not None else datetime.now(timezone.utc)
    if session_id is None:
        session_id = now.strftime("%Y-%m-%d_%H%M%S")
    if unrealized_pnl_krw is None:
        unrealized_pnl_krw = Decimal("0")

    return SessionRecord(
        session_id=session_id,
        timestamp=now,
        starting_capital_krw=starting_capital_krw,
        ending_capital_krw=ending_capital_krw,
        realized_pnl_krw=realized_pnl_krw,
        unrealized_pnl_krw=unrealized_pnl_krw,
        reconciliation_severity=reconciliation_severity,
        exception_count=exception_count,
        mode="live",
    )


# ---------------------------------------------------------------------------
# Public API — append_session_record
# ---------------------------------------------------------------------------


def append_session_record(
    audit_path: Path,
    record: SessionRecord,
    *,
    enforce_permissions: bool = True,
    create_if_missing: bool = True,
    fsync: bool = True,
) -> None:
    """한 줄 jsonl record 를 audit_path 에 atomic append.

    Args:
        audit_path: 기록 대상 jsonl 파일 (예: data/audit/sessions.jsonl).
        record: SessionRecord 인스턴스.
        enforce_permissions: True 면 0600 권한 강제 (layer 17).
        create_if_missing: True 면 파일/parent 자동 생성 + 0600 적용.
        fsync: True 면 write 후 fsync (best-effort, 실패 시 silent).

    Raises:
        SessionHistoryWriteRecordError: record 검증 또는 직렬화 실패.
        SessionHistoryWritePermissionError: 권한 위반.
        SessionHistoryWriteError: 파일 시스템 오류 등.

    Notes:
        - O_APPEND 사용 → 단일 호스트 다중 writer 도 한 줄 단위 atomic
          (한 줄 < PIPE_BUF 4096 bytes 보장 시).
        - finally 블록에서 호출하는 경우 호출자가 본 함수 예외를
          best-effort 로 catch 하여 shutdown 흐름 보존 권장.
    """
    if not isinstance(record, SessionRecord):
        raise SessionHistoryWriteRecordError(
            f"record must be SessionRecord, got {type(record).__name__}"
        )

    # parent 디렉토리 + 파일 생성
    if create_if_missing:
        try:
            audit_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SessionHistoryWriteError(
                f"parent directory 생성 실패: {audit_path.parent}: {exc}"
            ) from exc

        if not audit_path.exists():
            try:
                # touch + chmod 0600 즉시 적용
                audit_path.touch(exist_ok=False)
                os.chmod(audit_path, 0o600)
            except OSError as exc:
                raise SessionHistoryWriteError(
                    f"파일 생성/권한 설정 실패: {audit_path}: {exc}"
                ) from exc

    if not audit_path.exists():
        raise SessionHistoryWriteError(
            f"audit_path 미존재 (create_if_missing=False): {audit_path}"
        )

    # 권한 검증 (layer 17)
    if enforce_permissions:
        try:
            mode = audit_path.stat().st_mode & 0o777
        except OSError as exc:
            raise SessionHistoryWriteError(
                f"권한 확인 실패: {audit_path}: {exc}"
            ) from exc
        if mode != 0o600:
            raise SessionHistoryWritePermissionError(
                f"session history jsonl 권한이 0600 이 아님 "
                f"(path={audit_path}, actual={oct(mode)}). "
                f"chmod 600 으로 수정 후 재시도하십시오."
            )

    # JSON 직렬화 (record.to_jsonl_line() 안에서 size 검증)
    line = record.to_jsonl_line()

    # O_APPEND atomic write
    try:
        # 'a' 모드는 OS 레벨에서 O_APPEND 적용 — write 위치가 항상 EOF
        with audit_path.open("a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            if fsync:
                try:
                    os.fsync(f.fileno())
                except OSError:
                    # fsync 실패는 best-effort — 데이터 자체는 OS buffer 에
                    # 안전하게 들어갔으므로 swallow (drive 동기화 강도만 약화)
                    logger.debug("fsync failed (non-fatal) for %s", audit_path)
    except PermissionError as exc:
        raise SessionHistoryWritePermissionError(
            f"권한 거부 (write): {audit_path}: {exc}"
        ) from exc
    except OSError as exc:
        raise SessionHistoryWriteError(
            f"파일 write 실패: {audit_path}: {exc}"
        ) from exc

    logger.info(
        "session record appended: id=%s mode=%s path=%s",
        record.session_id,
        record.mode,
        audit_path,
    )


# ---------------------------------------------------------------------------
# Convenience: best-effort wrapper for finally blocks
# ---------------------------------------------------------------------------


def try_append_session_record(
    audit_path: Path,
    record: SessionRecord,
    *,
    enforce_permissions: bool = True,
    create_if_missing: bool = True,
) -> bool:
    """finally 블록 친화 wrapper — 예외를 catch + warning 로그 후 반환.

    `<model>` ESC/Ctrl-C 우선 정책 보존:
        runner shutdown 흐름은 본 함수 실패에 영향받지 않음.

    Returns:
        True 면 append 성공, False 면 실패 (warning 로그 출력됨).
    """
    try:
        append_session_record(
            audit_path,
            record,
            enforce_permissions=enforce_permissions,
            create_if_missing=create_if_missing,
        )
        return True
    except SessionHistoryWriteError as exc:
        logger.warning(
            "session history append failed (non-fatal): %s (path=%s, session_id=%s)",
            exc,
            audit_path,
            record.session_id if isinstance(record, SessionRecord) else "?",
        )
        return False
    except Exception as exc:  # noqa: BLE001 — 마지막 안전망
        logger.warning(
            "session history append unexpected error (non-fatal): %s", exc
        )
        return False


__all__ = (
    "SCHEMA_VERSION",
    "MAX_RECORD_LINE_BYTES",
    "SessionRecord",
    "SessionHistoryWriteError",
    "SessionHistoryWritePermissionError",
    "SessionHistoryWriteRecordError",
    "append_session_record",
    "try_append_session_record",
    "build_paper_session_record",
    "build_live_session_record",
)
