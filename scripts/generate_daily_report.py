#!/usr/bin/env python3
"""
일일 리포트 생성 CLI (Daily Report CLI)
=========================================

JCPR Trading System - jcpr-ts-v01
Task 49 v0.1

CLI에서 일일 리포트 생성. 의존 컴포넌트는 audit log + DB만 사용 (broker 호출 없음).

사용법 (Usage):
    python scripts/generate_daily_report.py \\
        --session-id session-2026-05-06 \\
        --starting-capital 10000000 \\
        --cash 9500000 \\
        --output-dir reports/2026-05-06 \\
        --positions-db data/local/positions.sqlite \\
        --ohlcv-db data/local/ohlcv.sqlite \\
        --quote-db data/local/quotes.sqlite \\
        --fills-db data/local/fills.sqlite \\
        --risk-audit data/audit/risk_decisions.jsonl \\
        --execution-audit data/audit/executions.jsonl \\
        --approval-audit data/audit/approvals.jsonl \\
        --formats json md html
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

KST = ZoneInfo("Asia/Seoul")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("daily_report_cli")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="JCPR 일일 리포트 생성 (Daily Report Generator) — Task 49 v0.1",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--starting-capital", type=str, required=True,
                        help="시작 자본 (KRW)")
    parser.add_argument("--cash", type=str, required=True,
                        help="현재 현금 (KRW)")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--session-date-kst", default=None,
                        help="KST 날짜 YYYY-MM-DD (기본: 오늘)")
    parser.add_argument("--session-start-utc", default=None,
                        help="ISO datetime (기본: 오늘 09:00 KST)")
    parser.add_argument("--session-end-utc", default=None,
                        help="ISO datetime (기본: 오늘 15:30 KST)")

    # DB 경로 (의존 컴포넌트 구성용)
    parser.add_argument("--positions-db", default=None)
    parser.add_argument("--ohlcv-db", default=None)
    parser.add_argument("--quote-db", default=None)
    parser.add_argument("--fills-db", default=None)
    parser.add_argument("--symbol-master-csv", default="data/reference/symbol_master.csv")

    # Audit 경로
    parser.add_argument("--risk-audit", default=None)
    parser.add_argument("--execution-audit", default=None)
    parser.add_argument("--approval-audit", default=None)

    # 출력
    parser.add_argument("--formats", nargs="+", default=["json", "md", "html"])

    # 옵션
    parser.add_argument(
        "--include-portfolio-risk", action="store_true",
        help="PortfolioRiskAnalyzer 사용 (Task 47 + Symbol Master 필요)",
    )

    args = parser.parse_args()

    # 세션 날짜
    if args.session_date_kst:
        session_date = date.fromisoformat(args.session_date_kst)
    else:
        session_date = datetime.now(KST).date()

    # 시작/종료 시각
    if args.session_start_utc:
        session_start = datetime.fromisoformat(args.session_start_utc)
        if session_start.tzinfo is None:
            logger.error("session_start_utc는 tz-aware 필수")
            return 1
    else:
        session_start = datetime.combine(
            session_date,
            datetime.strptime("09:00:00", "%H:%M:%S").time(),
        ).replace(tzinfo=KST).astimezone(timezone.utc)

    if args.session_end_utc:
        session_end = datetime.fromisoformat(args.session_end_utc)
        if session_end.tzinfo is None:
            logger.error("session_end_utc는 tz-aware 필수")
            return 1
    else:
        session_end = datetime.combine(
            session_date,
            datetime.strptime("15:30:00", "%H:%M:%S").time(),
        ).replace(tzinfo=KST).astimezone(timezone.utc)

    # 의존 컴포넌트 구성
    pnl_engine = None
    slippage_analyzer = None
    portfolio_risk_analyzer = None

    if args.positions_db and args.ohlcv_db:
        try:
            from src.data.ohlcv_store import OHLCVStore
            from src.data.quote_store import QuoteStore
            from src.pnl.pnl_engine import PnLEngine
            from src.pnl.position_ledger import PositionLedger
            from src.pnl.position_store import PositionStore

            ledger = PositionLedger(PositionStore(args.positions_db))
            ohlcv = OHLCVStore(args.ohlcv_db)
            quote = QuoteStore(args.quote_db) if args.quote_db else None
            pnl_engine = PnLEngine(ledger, ohlcv, quote_store=quote)
            logger.info("PnLEngine 구성 완료")
        except Exception as e:  # noqa: BLE001
            logger.error("PnLEngine 구성 실패: %s", e)

    if args.fills_db:
        try:
            from src.execution.fill_store import FillStore
            from src.pnl.slippage import SlippageAnalyzer

            slippage_analyzer = SlippageAnalyzer(FillStore(args.fills_db))
            logger.info("SlippageAnalyzer 구성 완료")
        except Exception as e:  # noqa: BLE001
            logger.error("SlippageAnalyzer 구성 실패: %s", e)

    if args.include_portfolio_risk and args.symbol_master_csv:
        try:
            from src.data.symbol_master import SymbolMaster
            from src.risk.portfolio_risk import PortfolioRiskAnalyzer

            sm = SymbolMaster.from_csv(args.symbol_master_csv)
            portfolio_risk_analyzer = PortfolioRiskAnalyzer(sm)
            logger.info("PortfolioRiskAnalyzer 구성 완료")
        except Exception as e:  # noqa: BLE001
            logger.error("PortfolioRiskAnalyzer 구성 실패: %s", e)

    # 빌드 + 저장
    from src.reports import DailyReportBuilder, DailyReportInputs

    inputs = DailyReportInputs(
        session_id=args.session_id,
        session_date_kst=session_date,
        starting_capital_krw=Decimal(args.starting_capital),
        cash_krw=Decimal(args.cash),
        session_start_utc=session_start,
        session_end_utc=session_end,
        pnl_engine=pnl_engine,
        slippage_analyzer=slippage_analyzer,
        portfolio_risk_analyzer=portfolio_risk_analyzer,
        reconciler=None,  # CLI에서는 broker 호출 없음
        risk_audit_path=Path(args.risk_audit) if args.risk_audit else None,
        execution_audit_path=Path(args.execution_audit) if args.execution_audit else None,
        approval_audit_path=Path(args.approval_audit) if args.approval_audit else None,
    )

    builder = DailyReportBuilder()
    paths = builder.build_and_save(
        inputs, args.output_dir, formats=args.formats,
    )

    print("\n--- 생성된 파일 (Generated Files) ---")
    for fmt, p in paths.items():
        print(f"  [{fmt}] {p}")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
