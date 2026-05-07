#!/usr/bin/env python3
"""
Market Agent CLI
=================

JCPR Trading System - jcpr-ts-v01
Task 37 v0.1

운영자가 명령줄에서 시장 요약 요청.
(Operator runs market summary from CLI.)

사용법 (Usage):
    # Mock LLM (테스트)
    python scripts/run_market_agent.py \\
        --starting-capital 10000000 --cash 500000

    # 자연어 query 추가
    python scripts/run_market_agent.py \\
        --starting-capital 10000000 --cash 500000 \\
        --query "오늘 어떻게 되어가나요?"

    # JSON 출력 (audit 통합용)
    python scripts/run_market_agent.py \\
        --starting-capital 10000000 --cash 500000 \\
        --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="JCPR Market Analyst Agent CLI (Task 37 v0.1)",
    )
    p.add_argument("--starting-capital", required=True,
                   help="세션 시작 자본 (KRW, Decimal-string)")
    p.add_argument("--cash", required=True,
                   help="현재 현금 (KRW, Decimal-string)")
    p.add_argument("--query", default="",
                   help="운영자 자연어 질문 (선택)")
    p.add_argument("--operator-id", default="operator-cli")
    p.add_argument("--session-id", default="session-cli")
    p.add_argument("--audit-dir", default="data/audit",
                   help="Audit 디렉터리")
    p.add_argument("--json", action="store_true", help="JSON 출력")
    p.add_argument("--llm", default="mock", choices=["mock"],
                   help="LLM 클라이언트 (현재 mock만 지원)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    args = _parse_args(argv)

    from src.agents import MarketAnalystAgent, MockLLMClient
    from src.observability import configure_default_writer

    # Audit 초기화
    configure_default_writer(args.audit_dir)

    # LLM 클라이언트
    if args.llm == "mock":
        llm = MockLLMClient(schema_based=True)
    else:
        print(f"❌ LLM '{args.llm}' not implemented", file=sys.stderr)
        return 1

    # Agent 실행
    agent = MarketAnalystAgent(
        llm_client=llm,
        operator_id=args.operator_id,
        session_id=args.session_id,
    )

    try:
        result = agent.summarize_market(
            starting_capital_krw=args.starting_capital,
            cash_krw=args.cash,
            operator_query=args.query,
        )
    except ValueError as e:
        print(f"❌ Validation error: {e}", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001
        print(f"❌ Agent error: {type(e).__name__}: {e}", file=sys.stderr)
        return 3

    # 출력
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2,
                         default=str))
    else:
        print("━━━ Market Analyst Result ━━━")
        print(f"Trace ID:       {result.trace_id}")
        print(f"Success:        {result.success}")
        print(f"Fallback used:  {result.fallback_used}")
        print(f"Tool calls:     {result.tool_calls_count}")
        print(f"LLM elapsed:    {result.llm_elapsed_ms:.0f} ms")
        print(f"Total elapsed:  {result.total_elapsed_ms:.0f} ms")
        print()
        print("─── 요약 (Summary) ───")
        print(result.summary_ko)
        print()
        if result.response and result.response.get("findings"):
            print("─── 발견 (Findings) ───")
            for i, finding in enumerate(result.response["findings"][:10], 1):
                stmt = finding.get("statement", "")
                src = finding.get("source", "")
                print(f"  {i}. {stmt} ({src})")
        if result.error:
            print()
            print(f"⚠ {result.error}")

    return 0 if result.success else 4


if __name__ == "__main__":
    sys.exit(main())
