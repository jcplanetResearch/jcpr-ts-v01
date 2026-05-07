#!/usr/bin/env python3
"""Task 38 — Risk Explanation Agent CLI runner.

Usage:
    python scripts/run_risk_agent.py \
        --starting-capital 100000000 \
        --current-cash 80000000 \
        --query "현재 위험 상태 요약"

Defaults to MockLLMClient + in-process MCPReadOnlyClient (no external network).
For production, supply a real LLMClient via --llm-impl module.path.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from decimal import Decimal
from pathlib import Path

# Ensure src/ on path
_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO))

# Block forbidden env keywords (defense in depth) ----------------------------
FORBIDDEN_ENV_KEYWORDS = ("PASSWORD", "SECRET", "TOKEN", "API_KEY",
                         "AUTH", "CREDENTIAL", "PRIVATE_KEY")


def _check_env_safe() -> None:
    """Refuse to run if env variables look like secrets are being passed in CLI."""
    for arg in sys.argv:
        upper = arg.upper()
        for kw in FORBIDDEN_ENV_KEYWORDS:
            if kw in upper and "=" in arg:
                print(f"ERROR: argument contains forbidden keyword '{kw}'. "
                      f"Secrets must not be passed via CLI.", file=sys.stderr)
                sys.exit(2)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Run Risk Explanation Agent (Task 38)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--starting-capital", type=str, required=True,
                   help="Starting capital in KRW (Decimal string)")
    p.add_argument("--current-cash", type=str, required=True,
                   help="Current cash in KRW (Decimal string)")
    p.add_argument("--query", type=str, required=True,
                   help="Operator query (Korean preferred)")
    p.add_argument("--max-tool-calls", type=int, default=5)
    p.add_argument("--output-format", choices=("json", "text"), default="text")
    p.add_argument("--audit-dir", type=str, default=None,
                   help="Optional audit directory (env JCPR_AUDIT_DIR overrides)")
    return p.parse_args()


def _format_text_report(report) -> str:
    """Pretty-print report for terminal display."""
    lines = []
    lines.append("=" * 70)
    lines.append("JCPR Risk Explanation Agent — Task 38")
    lines.append("=" * 70)
    lines.append(f"Trace ID:           {report.trace_id}")
    lines.append(f"Generated (UTC):    {report.generated_at_utc.isoformat()}")
    lines.append(f"Severity Overall:   {report.severity_overall.upper()}")
    lines.append(f"Breach Count:       {report.breach_count}")
    lines.append(f"Rejections (24h):   {report.rejection_count_24h}")
    lines.append(f"Schema Validated:   {report.schema_validated}")
    lines.append(f"Fallback Used:      {report.fallback_used}")
    lines.append(f"Elapsed:            {report.elapsed_ms}ms")
    lines.append("")
    lines.append("Summary (요약):")
    lines.append("-" * 70)
    lines.append(report.summary_kr)
    lines.append("")

    if report.action_candidates:
        lines.append("Suggested Actions (권고 액션 — ADVISORY ONLY, NOT EXECUTED):")
        lines.append("-" * 70)
        for i, c in enumerate(report.action_candidates, 1):
            lines.append(f"  [{i}] {c.tool_name} (severity={c.severity})")
            lines.append(f"      사유(rationale): {c.rationale_kr}")
            lines.append(f"      preview params: {dict(c.parameters_preview)}")
            lines.append(f"      ⚠ requires_human_approval={c.requires_human_approval}, "
                        f"executed={c.executed}")
            lines.append("")
        lines.append("⚠️  Operator must invoke Task 35 restricted MCP separately")
        lines.append("    under 3-phase approval workflow.")
    else:
        lines.append("No action candidates suggested.")
    lines.append("=" * 70)
    return "\n".join(lines)


def main() -> int:
    _check_env_safe()
    args = parse_args()

    try:
        starting = Decimal(args.starting_capital)
        cash = Decimal(args.current_cash)
    except Exception as e:
        print(f"ERROR: invalid Decimal arg: {e}", file=sys.stderr)
        return 2

    # Lazy imports (require full repo + Task 37 deps installed locally) -----
    try:
        from src.agents._llm_client import MockLLMClient
        from src.agents._mcp_client import MCPReadOnlyClient
        from src.agents.risk_agent import RiskExplanationAgent
    except ImportError as e:
        print(f"ERROR: missing dependencies — ensure Task 35-37 modules are "
              f"in src/. Detail: {e}", file=sys.stderr)
        return 3

    # Set up clients (production: replace MockLLMClient with real impl) -----
    llm = MockLLMClient()  # external SDK = 0 dependencies
    mcp = MCPReadOnlyClient()  # in-process bound to local DBs via env vars

    audit = None
    if args.audit_dir or os.getenv("JCPR_AUDIT_DIR"):
        try:
            from src.observability.audit_writer import AuditWriter
            audit_dir = args.audit_dir or os.getenv("JCPR_AUDIT_DIR")
            audit = AuditWriter(audit_dir)
        except ImportError:
            print("WARNING: AuditWriter unavailable; running without audit",
                  file=sys.stderr)

    agent = RiskExplanationAgent(
        llm_client=llm,
        mcp_client=mcp,
        audit_writer=audit,
        max_tool_calls=args.max_tool_calls,
    )

    report = agent.explain_risk(
        starting_capital_krw=starting,
        current_cash_krw=cash,
        operator_query=args.query,
    )

    if args.output_format == "json":
        out = {
            "trace_id": report.trace_id,
            "summary_kr": report.summary_kr,
            "severity_overall": report.severity_overall,
            "breach_count": report.breach_count,
            "rejection_count_24h": report.rejection_count_24h,
            "fallback_used": report.fallback_used,
            "schema_validated": report.schema_validated,
            "elapsed_ms": report.elapsed_ms,
            "generated_at_utc": report.generated_at_utc.isoformat(),
            "action_candidates": [
                {
                    "tool_name": c.tool_name,
                    "rationale_kr": c.rationale_kr,
                    "parameters_preview": dict(c.parameters_preview),
                    "severity": c.severity,
                    "requires_human_approval": c.requires_human_approval,
                    "executed": c.executed,
                }
                for c in report.action_candidates
            ],
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(_format_text_report(report))

    return 0 if report.severity_overall in ("info", "low") else 1


if __name__ == "__main__":
    sys.exit(main())
