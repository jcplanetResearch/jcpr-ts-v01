"""
일일 리포트 데이터 모델 + 출력 포맷
======================================

JCPR Trading System - jcpr-ts-v01
Task 49 v0.1

DailyReport (Final Output #1-12) + JSON / Markdown / HTML 출력.

원칙:
- frozen=True (immutable)
- Decimal → str (JSON 직렬화 안전)
- Korean(English) bilingual 헤더
- HTML은 단순 인라인 스타일 (외부 의존성 없음)
- 비밀 미포함
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional


def _json_default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, date):
        return o.isoformat()
    if isinstance(o, set):
        return sorted(o)
    raise TypeError(f"JSON 직렬화 불가: {type(o)}")


def _fmt_krw(value: Any) -> str:
    """KRW 정수 콤마 표기."""
    try:
        d = Decimal(str(value))
    except Exception:  # noqa: BLE001
        return str(value)
    sign = "-" if d < 0 else ""
    return f"{sign}{int(abs(d)):,} KRW"


def _fmt_pct(value: Any, decimals: int = 2) -> str:
    try:
        d = Decimal(str(value))
        return f"{d * 100:.{decimals}f}%"
    except Exception:  # noqa: BLE001
        return str(value)


# ─────────────────────────────────────────────────
# 입력 (의존 데이터 소스 모음)
# ─────────────────────────────────────────────────

@dataclass
class DailyReportInputs:
    """
    DailyReportBuilder의 입력.
    
    Builder 호출 시 의존성 인스턴스를 직접 받음 (DI).
    의존 인스턴스는 외부에서 구성하여 전달.
    """
    session_id: str
    session_date_kst: date
    starting_capital_krw: Decimal
    cash_krw: Decimal
    session_start_utc: datetime
    session_end_utc: datetime

    # 의존 데이터 소스 (옵션 — None이면 해당 항목 graceful degradation)
    pnl_engine: Any = None                       # Task 26 PnLEngine
    slippage_analyzer: Any = None                # Task 27 SlippageAnalyzer
    portfolio_risk_analyzer: Any = None          # Task 47 PortfolioRiskAnalyzer
    reconciler: Any = None                       # Task 28 Reconciler

    # Audit log paths (옵션)
    risk_audit_path: Optional[Path] = None       # Task 19 risk_decisions.jsonl
    execution_audit_path: Optional[Path] = None  # Task 21 executions.jsonl
    approval_audit_path: Optional[Path] = None   # Task 40 approvals.jsonl

    # 추가 옵션
    default_strategy_id: str = "momentum_v04"


# ─────────────────────────────────────────────────
# 리포트 (Final Output #1-12)
# ─────────────────────────────────────────────────

@dataclass(frozen=True)
class DailyReport:
    """
    일일 리포트 — Final Output #1-12 모두 포함.
    """
    metadata: dict[str, Any] = field(default_factory=dict)

    # Final Output items
    output_1_starting_capital: dict[str, Any] = field(default_factory=dict)
    output_2_ending_capital: dict[str, Any] = field(default_factory=dict)
    output_3_realized_pnl: dict[str, Any] = field(default_factory=dict)
    output_4_unrealized_pnl: dict[str, Any] = field(default_factory=dict)
    output_5_fees_slippage: dict[str, Any] = field(default_factory=dict)
    output_6_strategy_attribution: list[dict[str, Any]] = field(default_factory=list)
    output_7_symbol_attribution: dict[str, Any] = field(default_factory=dict)
    output_8_rejected_orders: dict[str, Any] = field(default_factory=dict)
    output_9_risk_limit_usage: dict[str, Any] = field(default_factory=dict)
    output_10_reconciliation_status: dict[str, Any] = field(default_factory=dict)
    output_11_exceptions: list[dict[str, Any]] = field(default_factory=list)
    output_12_next_session_capacity: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Dict 변환
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "metadata": dict(self.metadata),
            "output_1_starting_capital": dict(self.output_1_starting_capital),
            "output_2_ending_capital": dict(self.output_2_ending_capital),
            "output_3_realized_pnl": dict(self.output_3_realized_pnl),
            "output_4_unrealized_pnl": dict(self.output_4_unrealized_pnl),
            "output_5_fees_slippage": dict(self.output_5_fees_slippage),
            "output_6_strategy_attribution": list(self.output_6_strategy_attribution),
            "output_7_symbol_attribution": dict(self.output_7_symbol_attribution),
            "output_8_rejected_orders": dict(self.output_8_rejected_orders),
            "output_9_risk_limit_usage": dict(self.output_9_risk_limit_usage),
            "output_10_reconciliation_status": dict(self.output_10_reconciliation_status),
            "output_11_exceptions": list(self.output_11_exceptions),
            "output_12_next_session_capacity": dict(self.output_12_next_session_capacity),
        }

    # ------------------------------------------------------------------
    # JSON
    # ------------------------------------------------------------------

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(
            self.to_dict(),
            default=_json_default,
            ensure_ascii=False,
            indent=indent,
        )

    # ------------------------------------------------------------------
    # Markdown
    # ------------------------------------------------------------------

    def to_markdown(self) -> str:
        lines: list[str] = []
        m = self.metadata

        # Header
        lines.append(f"# 일일 트레이딩 리포트 (Daily Trading Report)")
        lines.append(f"")
        lines.append(f"**세션 ID (Session)**: `{m.get('session_id', 'unknown')}`")
        lines.append(f"**세션 날짜 (KST)**: {m.get('session_date_kst', 'unknown')}")
        lines.append(f"**기간 (Period)**: {m.get('session_start_utc', '?')} ~ {m.get('session_end_utc', '?')}")
        lines.append(f"**생성 시각 (Generated)**: {m.get('generated_at_utc', 'unknown')}")
        lines.append(f"**시스템 (System)**: jcpr-ts-v01 — Task 49 v{m.get('report_version', '0.1')}")
        lines.append(f"")
        lines.append("---")
        lines.append("")

        # 1. Starting Capital
        lines.append("## 1. 시작 자본 (Starting Capital)")
        lines.append("")
        s1 = self.output_1_starting_capital
        lines.append(f"- **시작 자본**: {_fmt_krw(s1.get('starting_capital_krw', 0))}")
        lines.append(f"- **시작 현금**: {_fmt_krw(s1.get('starting_cash_krw', 0))}")
        lines.append("")

        # 2. Ending Capital
        lines.append("## 2. 종료 자본 (Ending Capital)")
        lines.append("")
        s2 = self.output_2_ending_capital
        lines.append(f"- **종료 자본**: {_fmt_krw(s2.get('ending_capital_krw', 0))}")
        lines.append(f"- **종료 현금**: {_fmt_krw(s2.get('cash_krw', 0))}")
        lines.append(f"- **시장 평가액**: {_fmt_krw(s2.get('total_market_value_krw', 0))}")
        ret = s2.get("return_pct")
        if ret is not None:
            lines.append(f"- **수익률 (Return)**: {_fmt_pct(ret)}")
        lines.append("")

        # 3. Realized P&L
        lines.append("## 3. 실현 손익 (Realized P&L)")
        lines.append("")
        s3 = self.output_3_realized_pnl
        lines.append(f"- **누적 실현 손익**: {_fmt_krw(s3.get('total_realized_pnl_krw', 0))}")
        lines.append("")

        # 4. Unrealized P&L
        lines.append("## 4. 미실현 손익 (Unrealized P&L)")
        lines.append("")
        s4 = self.output_4_unrealized_pnl
        lines.append(f"- **미실현 손익**: {_fmt_krw(s4.get('total_unrealized_pnl_krw', 0))}")
        stale = s4.get("stale_symbols", [])
        if stale:
            lines.append(f"- ⚠️ **가격 미상 종목 (stale)**: {', '.join(stale)}")
        lines.append("")

        # 5. Fees & Slippage
        lines.append("## 5. 수수료 + 슬리피지 (Fees & Slippage)")
        lines.append("")
        s5 = self.output_5_fees_slippage
        lines.append(f"- **수수료 합계 (Fees)**: {_fmt_krw(s5.get('total_fees_krw', 0))}")
        lines.append(f"- **거래세 합계 (Taxes)**: {_fmt_krw(s5.get('total_taxes_krw', 0))}")
        slip = s5.get("slippage", {})
        if slip and slip.get("count", 0) > 0:
            lines.append(f"- **슬리피지 분석 건수**: {slip.get('count', 0)}건")
            lines.append(f"- **평균 슬리피지 (avg)**: {slip.get('avg_slippage_bps', 'N/A')} bps")
            lines.append(f"- **중간값 슬리피지 (median)**: {slip.get('median_slippage_bps', 'N/A')} bps")
            lines.append(f"- **p95 슬리피지**: {slip.get('p95_slippage_bps', 'N/A')} bps")
            lines.append(f"- **불리한 슬리피지 비율**: {slip.get('unfavorable_pct', 'N/A')}")
            lines.append(f"- **부분 체결 건수**: {slip.get('partial_fill_count', 0)}건")
        else:
            lines.append("- 슬리피지 분석 데이터 없음 (no fills with audit data)")
        lines.append("")

        # 6. Strategy Attribution
        lines.append("## 6. 전략별 귀속 (Strategy Attribution)")
        lines.append("")
        if self.output_6_strategy_attribution:
            lines.append("| 전략 (Strategy) | 실현 P&L | 미실현 P&L | 체결 수 | 종목 수 |")
            lines.append("|---|---:|---:|---:|---:|")
            for s in self.output_6_strategy_attribution:
                lines.append(
                    f"| `{s.get('strategy_id', '?')}` "
                    f"| {_fmt_krw(s.get('realized_pnl_krw', 0))} "
                    f"| {_fmt_krw(s.get('unrealized_pnl_krw', 0))} "
                    f"| {s.get('fills_count', 0)} "
                    f"| {len(s.get('symbols', []))} |"
                )
        else:
            lines.append("- 전략 데이터 없음")
        lines.append("")

        # 7. Symbol Attribution
        lines.append("## 7. 종목별 귀속 (Symbol Attribution)")
        lines.append("")
        sym_data = self.output_7_symbol_attribution
        if sym_data:
            lines.append("| 종목 | 수량 | 평균가 | 현재가 | 실현 P&L | 미실현 P&L |")
            lines.append("|---|---:|---:|---:|---:|---:|")
            for sym, info in sorted(sym_data.items()):
                qty = info.get("quantity", 0)
                avg = info.get("avg_cost_krw", "?")
                cur = info.get("current_price_krw") or "stale"
                rea = info.get("realized_krw", 0)
                unrea = info.get("unrealized_krw") or "N/A"
                lines.append(
                    f"| `{sym}` | {qty:,} "
                    f"| {_fmt_krw(avg) if avg != '?' else '?'} "
                    f"| {_fmt_krw(cur) if cur != 'stale' else '⚠️ stale'} "
                    f"| {_fmt_krw(rea)} "
                    f"| {_fmt_krw(unrea) if unrea != 'N/A' else 'N/A'} |"
                )
        else:
            lines.append("- 종목 데이터 없음")
        lines.append("")

        # 8. Rejected Orders
        lines.append("## 8. 거부된 주문 (Rejected Orders)")
        lines.append("")
        s8 = self.output_8_rejected_orders
        lines.append(f"- **총 평가 (Total)**: {s8.get('total', 0)}건")
        lines.append(f"- **거부 (Rejected)**: {s8.get('reject_count', 0)}건 ({_fmt_pct(s8.get('rejection_rate', 0))})")
        lines.append(f"- **통과 (Pass)**: {s8.get('pass_count', 0)}건")
        by_gate = s8.get("by_gate_reject", {})
        if by_gate:
            lines.append("")
            lines.append("**게이트별 거부 분포:**")
            for gate, cnt in sorted(by_gate.items(), key=lambda x: -x[1]):
                lines.append(f"  - `{gate}`: {cnt}건")
        lines.append("")

        # 9. Risk-Limit Usage
        lines.append("## 9. 리스크 한도 사용량 (Risk-Limit Usage)")
        lines.append("")
        s9 = self.output_9_risk_limit_usage
        port = s9.get("portfolio", {})
        if port:
            lines.append("**포트폴리오 노출:**")
            lines.append(f"  - 전체 노출: {_fmt_pct(port.get('total_exposure_pct', 0))}")
            sectors = port.get("by_sector_exposure_pct", {})
            if sectors:
                lines.append(f"  - 섹터별:")
                for sec, pct in sorted(sectors.items(), key=lambda x: -Decimal(str(x[1]))):
                    lines.append(f"    - `{sec}`: {_fmt_pct(pct)}")
            warnings = port.get("warnings", [])
            if warnings:
                lines.append("  - **경고:**")
                for w in warnings:
                    lines.append(f"    - {w}")
        gate_pass = s9.get("gate_pass_rates", {})
        if gate_pass:
            lines.append("")
            lines.append("**게이트별 통과율:**")
            for gate, rate in sorted(gate_pass.items()):
                lines.append(f"  - `{gate}`: {_fmt_pct(rate)}")
        lines.append("")

        # 10. Reconciliation
        lines.append("## 10. 정합성 점검 (Reconciliation)")
        lines.append("")
        s10 = self.output_10_reconciliation_status
        if s10.get("performed"):
            sev = s10.get("severity", "ok")
            sev_emoji = {"ok": "✅", "minor": "⚠️", "major": "🔴"}.get(sev, "❓")
            lines.append(f"- **심각도 (Severity)**: {sev_emoji} `{sev}`")
            lines.append(f"- **일치 종목**: {s10.get('match_count', 0)}건")
            lines.append(f"- **불일치 종목**: {s10.get('mismatch_count', 0)}건")
            by_type = s10.get("by_type", {})
            if by_type:
                for t, c in by_type.items():
                    lines.append(f"  - `{t}`: {c}건")
        else:
            reason = s10.get("reason", "정합성 점검 미수행")
            lines.append(f"- ⚠️ {reason}")
        lines.append("")

        # 11. Exceptions
        lines.append("## 11. 예외 (Exceptions)")
        lines.append("")
        if self.output_11_exceptions:
            lines.append(f"**총 예외 건수**: {len(self.output_11_exceptions)}건")
            lines.append("")
            for i, exc in enumerate(self.output_11_exceptions[:20], start=1):  # 최대 20건만 표시
                src = exc.get("source", "?")
                ts = exc.get("timestamp_utc", "?")
                msg = exc.get("message", "?")
                lines.append(f"{i}. [{src}] `{ts}`: {msg}")
            if len(self.output_11_exceptions) > 20:
                lines.append(f"")
                lines.append(f"... 외 {len(self.output_11_exceptions) - 20}건 (전체는 JSON 참조)")
        else:
            lines.append("✅ 예외 없음 (no exceptions)")
        lines.append("")

        # 12. Next Capacity Recommendation
        lines.append("## 12. 다음 세션 자본 추천 (Next-Session Capacity Recommendation)")
        lines.append("")
        s12 = self.output_12_next_session_capacity
        sev = s12.get("severity", "ok")
        sev_emoji = {
            "ok": "✅", "low": "⚠️", "moderate": "⚠️",
            "high": "🔴", "critical": "🚨",
        }.get(sev, "❓")
        lines.append(f"- **상태 (Severity)**: {sev_emoji} `{sev}`")
        lines.append(f"- **현재 자본 (Current)**: {_fmt_krw(s12.get('current_capital_krw', 0))}")
        lines.append(f"- **권장 비율**: {_fmt_pct(s12.get('recommend_pct', 0))}")
        lines.append(f"- **권장 자본 (Recommended)**: {_fmt_krw(s12.get('recommended_capacity_krw', 0))}")
        lines.append(f"- **위험 신호 합계 (Risk Signals)**: {s12.get('risk_signals', 0)}")
        breakdown = s12.get("risk_signal_breakdown", {})
        if breakdown:
            lines.append(f"  - 분포:")
            for k, v in breakdown.items():
                lines.append(f"    - `{k}`: {v}")
        reasoning = s12.get("reasoning", [])
        if reasoning:
            lines.append("")
            lines.append("**근거 (Reasoning):**")
            for r in reasoning:
                lines.append(f"- {r}")
        lines.append("")

        lines.append("---")
        lines.append("")
        lines.append("_⚠ Final Output #1-12 모두 포함된 자동 생성 리포트입니다._")
        lines.append("_(Auto-generated report covering all 12 final output items.)_")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # HTML
    # ------------------------------------------------------------------

    def to_html(self) -> str:
        """
        간단한 HTML — 외부 의존성 없이 단일 파일.
        브라우저에서 PDF 인쇄 가능.
        """
        md = self.to_markdown()
        # Markdown을 그대로 <pre>에 넣지 않고 간단한 변환
        # 실용성을 위해 표/제목/링크 정도만 변환
        body = self._markdown_to_html_simple(md)

        m = self.metadata
        title = (
            f"Daily Report — {html.escape(str(m.get('session_id', '')))} "
            f"({html.escape(str(m.get('session_date_kst', '')))})"
        )

        return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Apple SD Gothic Neo",
                 "Malgun Gothic", "Helvetica Neue", Arial, sans-serif;
    line-height: 1.6; max-width: 960px; margin: 2em auto; padding: 1em;
    color: #222; background: #fafafa;
  }}
  h1 {{ border-bottom: 3px solid #2c5282; padding-bottom: 0.3em; color: #2c5282; }}
  h2 {{ border-bottom: 1px solid #e2e8f0; padding-bottom: 0.2em; margin-top: 2em; color: #2d3748; }}
  table {{ border-collapse: collapse; width: 100%; margin: 1em 0; }}
  th, td {{ border: 1px solid #cbd5e0; padding: 6px 10px; text-align: left; }}
  th {{ background: #edf2f7; }}
  td {{ background: white; }}
  code {{ background: #edf2f7; padding: 2px 6px; border-radius: 3px;
          font-family: "Menlo", "Consolas", monospace; font-size: 0.92em; }}
  ul, ol {{ margin-left: 1.4em; }}
  li {{ margin: 0.3em 0; }}
  hr {{ border: none; border-top: 1px solid #cbd5e0; margin: 2em 0; }}
  em {{ color: #4a5568; }}
  .footer {{ font-size: 0.85em; color: #718096; margin-top: 3em;
             border-top: 1px solid #e2e8f0; padding-top: 1em; }}
  @media print {{
    body {{ background: white; max-width: 100%; }}
    h2 {{ page-break-before: auto; }}
  }}
</style>
</head>
<body>
{body}
<div class="footer">
  Generated by jcpr-ts-v01 — Task 49 v0.1 — {html.escape(str(m.get('generated_at_utc', '')))}
</div>
</body>
</html>"""

    @staticmethod
    def _markdown_to_html_simple(md: str) -> str:
        """매우 간단한 Markdown → HTML 변환 (외부 라이브러리 없이)."""
        out_lines: list[str] = []
        in_table = False
        table_header_done = False

        def flush_table():
            nonlocal in_table, table_header_done
            if in_table:
                out_lines.append("</tbody></table>")
                in_table = False
                table_header_done = False

        for raw_line in md.split("\n"):
            line = raw_line.rstrip()

            # 테이블
            if line.startswith("|") and line.endswith("|"):
                cells = [c.strip() for c in line[1:-1].split("|")]
                # 구분선 (---) 인지
                if all(set(c) <= set("-:") for c in cells):
                    if in_table and not table_header_done:
                        out_lines.append("</thead><tbody>")
                        table_header_done = True
                    continue
                if not in_table:
                    out_lines.append('<table><thead>')
                    in_table = True
                    table_header_done = False
                tag = "th" if not table_header_done else "td"
                row = "<tr>" + "".join(
                    f"<{tag}>{DailyReport._inline(c)}</{tag}>" for c in cells
                ) + "</tr>"
                out_lines.append(row)
                continue
            else:
                flush_table()

            # 헤딩
            if line.startswith("# "):
                out_lines.append(f"<h1>{DailyReport._inline(line[2:])}</h1>")
            elif line.startswith("## "):
                out_lines.append(f"<h2>{DailyReport._inline(line[3:])}</h2>")
            elif line.startswith("### "):
                out_lines.append(f"<h3>{DailyReport._inline(line[4:])}</h3>")
            elif line == "---":
                out_lines.append("<hr>")
            elif line.startswith("  - ") or line.startswith("    - "):
                indent = "  " if line.startswith("  - ") else "    "
                content = line[len(indent) + 2:]
                out_lines.append(f"<li style=\"margin-left:{len(indent)}em\">{DailyReport._inline(content)}</li>")
            elif line.startswith("- "):
                out_lines.append(f"<li>{DailyReport._inline(line[2:])}</li>")
            elif line and line[0].isdigit() and ". " in line[:5]:
                _, rest = line.split(". ", 1)
                out_lines.append(f"<li>{DailyReport._inline(rest)}</li>")
            elif line == "":
                out_lines.append("")
            else:
                out_lines.append(f"<p>{DailyReport._inline(line)}</p>")

        flush_table()

        # <li> 그룹을 <ul>로 감싸기 (간단)
        result = []
        in_ul = False
        for ln in out_lines:
            if ln.startswith("<li"):
                if not in_ul:
                    result.append("<ul>")
                    in_ul = True
                result.append(ln)
            else:
                if in_ul:
                    result.append("</ul>")
                    in_ul = False
                result.append(ln)
        if in_ul:
            result.append("</ul>")

        return "\n".join(result)

    @staticmethod
    def _inline(text: str) -> str:
        """인라인 변환 — bold, code."""
        # 먼저 escape
        s = html.escape(text)
        # **bold** — 간단히 처리 (욕심 안 부리고)
        import re
        s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
        # `code`
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        # _italic_
        s = re.sub(r"_([^_]+)_", r"<em>\1</em>", s)
        return s

    # ------------------------------------------------------------------
    # 파일 저장
    # ------------------------------------------------------------------

    def save_json(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.to_json(), encoding="utf-8")
        return p

    def save_markdown(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.to_markdown(), encoding="utf-8")
        return p

    def save_html(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.to_html(), encoding="utf-8")
        return p
