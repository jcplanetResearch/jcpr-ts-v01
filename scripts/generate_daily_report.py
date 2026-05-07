#!/usr/bin/env python3
"""
일일 리포트 생성 CLI (Daily Report Generation CLI)
====================================================

JCPR Trading System - jcpr-ts-v01
Task 49 v0.2

사용 (Usage):
    python scripts/generate_daily_report.py \\
        --session-id session-2026-05-07 \\
        --date 2026-05-07 \\
        --starting-capital 10000000 \\
        --cash 9500000 \\
        --output-dir reports/2026-05-07

또는 환경변수 활용:
    export JCPR_POSITIONS_DB=data/db/positions.db
    export JCPR_OHLCV_DB=data/db/ohlcv.db
    export JCPR_RISK_AUDIT=data/audit/risk_decisions.jsonl
    export JCPR_EXEC_AUDIT=data/audit/executions.jsonl
    python scripts/generate_daily_report.py --date 2026-05-07 \\
        --starting-capital 10000000 --cash 9500000

보안 (Security):
    - 자격증명 인자 거부 — --password, --token 등 인자 사용 시 즉시 종료
    - 데이터 경로는 환경변수 우선
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

# repo path
_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from src.reports import DailyReportBuilder, DailyReportInputs  # noqa: E402


# ─────────────────────────────────────────────────
# 보안: 시크릿 인자 거부
# ─────────────────────────────────────────────────

_FORBIDDEN_ARG_KEYWORDS = (
    "password", "secret", "token", "api-key", "apikey",
    "credential", "auth-key", "private-key",
)


def _check_no_secret_args(argv: list[str]) -> None:
    """명령줄 인자에 시크릿성 키워드 발견 시 즉시 종료."""
    for arg in argv:
        low = arg.lower()
        for kw in _FORBIDDEN_ARG_KEYWORDS:
            if kw in low:
                print(
                    f"❌ 보안 오류 (Security error): '{kw}' 키워드 인자 사용 금지.\n"
                    f"   자격증명은 환경변수나 설정파일로만 전달하세요.\n"
                    f"   (Credentials must be passed via env vars or config files only.)",
                    file=sys.stderr,
                )
                sys.exit(2)


# ─────────────────────────────────────────────────
# 인자 파싱
# ─────────────────────────────────────────────────

def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="JCPR Daily Report Generator (Task 49 v0.2)",
    )
    p.add_argument("--session-id", required=False,
                   help="세션 ID (기본: session-{date})")
    p.add_argument("--date", required=True,
                   help="세션 날짜 KST (YYYY-MM-DD)")
    p.add_argument("--starting-capital", required=True, type=Decimal,
                   help="시작 자본 KRW (Final Output #1)")
    p.add_argument("--cash", required=True, type=Decimal,
                   help="현금 잔고 KRW")
    p.add_argument("--output-dir", default=None,
                   help="출력 디렉터리 (기본: reports/{date})")
    p.add_argument("--formats", default="json,md,html",
                   help="출력 포맷 콤마구분 (기본: json,md,html)")
    # 데이터 소스 — 환경변수 fallback
    p.add_argument("--positions-db",
                   default=os.environ.get("JCPR_POSITIONS_DB"))
    p.add_argument("--ohlcv-db",
                   default=os.environ.get("JCPR_OHLCV_DB"))
    p.add_argument("--quote-db",
                   default=os.environ.get("JCPR_QUOTE_DB"))
    p.add_argument("--risk-audit",
                   default=os.environ.get("JCPR_RISK_AUDIT"))
    p.add_argument("--execution-audit",
                   default=os.environ.get("JCPR_EXEC_AUDIT"))
    p.add_argument("--approval-audit",
                   default=os.environ.get("JCPR_APPROVAL_AUDIT"))
    return p.parse_args(argv)


# ─────────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    _check_no_secret_args(argv)
    args = _parse_args(argv)

    # 세션 날짜 파싱
    try:
        session_date = date.fromisoformat(args.date)
    except ValueError:
        print(f"❌ Invalid --date: {args.date}", file=sys.stderr)
        return 2

    session_id = args.session_id or f"session-{args.date}"

    # KST 0시 → UTC, KST 23:59:59 → UTC
    kst = timezone(timedelta(hours=9))
    session_start_kst = datetime.combine(session_date, time(0, 0, 0), tzinfo=kst)
    session_end_kst = datetime.combine(session_date, time(23, 59, 59), tzinfo=kst)
    session_start_utc = session_start_kst.astimezone(timezone.utc)
    session_end_utc = session_end_kst.astimezone(timezone.utc)

    # 출력 디렉터리
    output_dir = args.output_dir or f"reports/{args.date}"

    # 포맷 파싱
    formats = tuple(f.strip() for f in args.formats.split(",") if f.strip())

    # 입력 조립
    inputs = DailyReportInputs(
        session_id=session_id,
        session_date_kst=session_date,
        session_start_utc=session_start_utc,
        session_end_utc=session_end_utc,
        starting_capital_krw=args.starting_capital,
        cash_krw=args.cash,
        positions_db=args.positions_db,
        ohlcv_db=args.ohlcv_db,
        quote_db=args.quote_db,
        risk_audit_path=args.risk_audit,
        execution_audit_path=args.execution_audit,
        approval_audit_path=args.approval_audit,
    )

    print(f"📊 일일 리포트 생성 (Generating daily report)")
    print(f"   세션 (Session): {session_id}")
    print(f"   날짜 (Date KST): {args.date}")
    print(f"   시작 자본: {int(args.starting_capital):,} KRW")
    print(f"   출력 (Output): {output_dir}")
    print(f"   포맷 (Formats): {', '.join(formats)}")
    print()

    builder = DailyReportBuilder()
    try:
        paths = builder.build_and_save(inputs, output_dir, formats=formats)
    except Exception as e:  # noqa: BLE001
        print(f"❌ 리포트 생성 실패: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print("✅ 생성 완료 (Generated):")
    for fmt, p in paths.items():
        print(f"   {fmt:5s}: {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
