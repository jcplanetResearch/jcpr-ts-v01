#!/usr/bin/env python3
"""
리스크 거부 분석 CLI (Risk Rejection Analysis CLI)
====================================================

JCPR Trading System - jcpr-ts-v01
Task 20 v0.1

Task 19 risk_decisions.jsonl audit log 분석 → 리포트 생성.

사용법 (Usage):
    python scripts/analyze_rejections.py \\
        --risk-audit data/audit/risk_decisions.jsonl \\
        --output-dir reports/rejections/2026-05-06 \\
        --formats json md html csv \\
        --window-minutes 30

    # 시간 범위 필터
    python scripts/analyze_rejections.py \\
        --risk-audit data/audit/risk_decisions.jsonl \\
        --since 2026-05-06T00:00:00+00:00 \\
        --until 2026-05-06T23:59:59+00:00 \\
        --output-dir reports/rejections/2026-05-06
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("rejection_cli")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="JCPR 리스크 거부 분석 (Risk Rejection Analysis) — Task 20 v0.1",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--risk-audit", required=True,
                        help="Task 19 risk_decisions.jsonl 경로")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--since", default=None,
                        help="ISO datetime — 이 시각 이후만 분석")
    parser.add_argument("--until", default=None,
                        help="ISO datetime — 이 시각 이전만 분석")
    parser.add_argument("--window-minutes", type=int, default=30,
                        help="추세 윈도우 크기 (분)")
    parser.add_argument("--formats", nargs="+",
                        default=["json", "md", "html", "csv"])

    # 진단 임계값 override
    parser.add_argument("--rate-limit-threshold", type=float, default=None)
    parser.add_argument("--exposure-threshold", type=float, default=None)
    parser.add_argument("--single-symbol-dominance", type=float, default=None)

    args = parser.parse_args()

    # 시간 파싱
    since_utc = None
    until_utc = None
    if args.since:
        since_utc = datetime.fromisoformat(args.since)
        if since_utc.tzinfo is None:
            logger.error("--since는 tz-aware 필수")
            return 1
    if args.until:
        until_utc = datetime.fromisoformat(args.until)
        if until_utc.tzinfo is None:
            logger.error("--until은 tz-aware 필수")
            return 1

    # 임계값 override
    thresholds = {}
    if args.rate_limit_threshold is not None:
        thresholds["rate_limit_concern_pct"] = args.rate_limit_threshold
    if args.exposure_threshold is not None:
        thresholds["exposure_concern_pct"] = args.exposure_threshold
    if args.single_symbol_dominance is not None:
        thresholds["single_symbol_dominance_pct"] = args.single_symbol_dominance

    from src.reports import RejectionAnalyzer

    analyzer = RejectionAnalyzer(
        window_minutes=args.window_minutes,
        thresholds=thresholds if thresholds else None,
    )
    paths = analyzer.analyze_and_save(
        args.risk_audit,
        args.output_dir,
        since_utc=since_utc,
        until_utc=until_utc,
        formats=tuple(args.formats),
    )

    print("\n--- 생성된 파일 (Generated Files) ---")
    for fmt, p in paths.items():
        print(f"  [{fmt}] {p}")

    # 진단 요약 표시
    report = analyzer.analyze(
        args.risk_audit, since_utc=since_utc, until_utc=until_utc,
    )
    print(f"\n총 평가: {report.total_evaluations:,}건, "
          f"거부: {report.reject_count:,}건 "
          f"({report.rejection_rate:.2%})")
    if report.diagnostic_findings:
        print("\n--- 진단 (Diagnostics) ---")
        for f in report.diagnostic_findings:
            severity_label = {
                "critical": "[CRITICAL]",
                "warning": "[WARNING]",
                "info": "[INFO]",
            }.get(f.severity, "[?]")
            print(f"  {severity_label} {f.message}")
    print()

    return 0 if not report.has_critical_findings() else 2


if __name__ == "__main__":
    sys.exit(main())
