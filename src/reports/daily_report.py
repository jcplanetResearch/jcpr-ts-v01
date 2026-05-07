"""
일일 리포트 모델 (Daily Report Model)
======================================

JCPR Trading System - jcpr-ts-v01
Task 49 v0.2

DailyReport dataclass + 3포맷 (JSON/MD/HTML) 직렬화.

Final Output 매핑:
    #1 starting_capital
    #2 ending_capital
    #3 realized_pnl
    #4 unrealized_pnl
    #5 fees_slippage
    #6 strategy_attribution
    #7 symbol_attribution
    #8 rejected_orders
    #9 risk_limit_usage
    #10 reconciliation_status
    #11 exceptions
    #12 next_session_capacity
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from html import escape
from pathlib import Path
from typing import Any, Optional


# ─────────────────────────────────────────────────
# 입력 모델 (Input Model)
# ─────────────────────────────────────────────────

@dataclass(frozen=True)
class DailyReportInputs:
    """
    일일 리포트 생성 입력.

    Task 48 DashboardDataSource와 동일 키 컨벤션 사용.
    """
    # 세션 메타
    session_id: str
    session_date_kst: date
    session_start_utc: datetime
    session_end_utc: datetime

    # 자본 (Capital)
    starting_capital_krw: Decimal
    cash_krw: Decimal

    # 데이터 소스 — Task 48 DashboardDataSource와 호환
    positions_db: Optional[str] = None
    ohlcv_db: Optional[str] = None
    quote_db: Optional[str] = None
    risk_audit_path: Optional[str] = None
    execution_audit_path: Optional[str] = None
    approval_audit_path: Optional[str] = None
    reconciliation_audit_path: Optional[str] = None

    # 선택적: 외부 reconciliation 결과 주입
    reconciliation_status: Optional[dict[str, Any]] = None
    # 선택적: 외부 portfolio risk 경고 주입
    portfolio_risk_warnings: list[str] = field(default_factory=list)

    def __post_init__(self):
        # tz-aware 강제
        for fld in ("session_start_utc", "session_end_utc"):
            v = getattr(self, fld)
            if v.tzinfo is None:
                raise ValueError(f"{fld} must be tz-aware UTC datetime")

    def __repr__(self) -> str:
        # 시크릿 가능 필드 마스킹은 없지만 경로는 전체 노출 안 함
        return (
            f"DailyReportInputs(session_id={self.session_id!r}, "
            f"date={self.session_date_kst.isoformat()}, "
            f"capital={self.starting_capital_krw})"
        )


# ─────────────────────────────────────────────────
# 메인 리포트 (Main Report)
# ─────────────────────────────────────────────────

@dataclass(frozen=True)
class DailyReport:
    """
    완성된 일일 리포트 — Final Output #1-12.

    모든 숫자는 직렬화 안전을 위해 str(Decimal)로 저장.
    """
    metadata: dict[str, Any]
    output_1_starting_capital: dict[str, Any]
    output_2_ending_capital: dict[str, Any]
    output_3_realized_pnl: dict[str, Any]
    output_4_unrealized_pnl: dict[str, Any]
    output_5_fees_slippage: dict[str, Any]
    output_6_strategy_attribution: list[dict[str, Any]]
    output_7_symbol_attribution: dict[str, Any]
    output_8_rejected_orders: dict[str, Any]
    output_9_risk_limit_usage: dict[str, Any]
    output_10_reconciliation_status: dict[str, Any]
    output_11_exceptions: list[dict[str, Any]]
    output_12_next_session_capacity: dict[str, Any]

    # ─── JSON 직렬화 ──────────────────────────
    def to_dict(self) -> dict[str, Any]:
        """전체 dict 표현 — JSON 직렬화에 직접 사용."""
        return {
            "metadata": self.metadata,
            "final_outputs": {
                "1_starting_capital": self.output_1_starting_capital,
                "2_ending_capital": self.output_2_ending_capital,
                "3_realized_pnl": self.output_3_realized_pnl,
                "4_unrealized_pnl": self.output_4_unrealized_pnl,
                "5_fees_slippage": self.output_5_fees_slippage,
                "6_strategy_attribution": self.output_6_strategy_attribution,
                "7_symbol_attribution": self.output_7_symbol_attribution,
                "8_rejected_orders": self.output_8_rejected_orders,
                "9_risk_limit_usage": self.output_9_risk_limit_usage,
                "10_reconciliation_status": self.output_10_reconciliation_status,
                "11_exceptions": self.output_11_exceptions,
                "12_next_session_capacity": self.output_12_next_session_capacity,
            },
        }

    def to_json(self, *, indent: int = 2) -> str:
        """JSON 문자열."""
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            indent=indent,
            default=_json_default,
        )

    # ─── Markdown 직렬화 ──────────────────────
    def to_markdown(self) -> str:
        """Markdown 문자열 — GitHub/사람 읽기용."""
        m = self.metadata
        lines: list[str] = []
        lines.append(f"# 일일 거래 리포트 (Daily Trading Report)")
        lines.append(f"")
        lines.append(f"**세션 (Session)**: `{m.get('session_id', '?')}`  ")
        lines.append(f"**일자 (Date KST)**: {m.get('session_date_kst', '?')}  ")
        lines.append(f"**기간 (Period UTC)**: "
                     f"{m.get('session_start_utc', '?')} ~ {m.get('session_end_utc', '?')}  ")
        lines.append(f"**생성 (Generated)**: {m.get('generated_at_utc', '?')}  ")
        lines.append(f"**버전 (Version)**: Task 49 v{m.get('report_version', '?')}")
        lines.append("")
        lines.append("---")
        lines.append("")

        # ─── #1 시작 자본 ─────────────────────
        lines.append("## #1 시작 자본 (Starting Capital)")
        lines.append("")
        lines.append(f"- **금액 (Amount)**: {_fmt_krw(self.output_1_starting_capital.get('amount_krw'))}")
        lines.append("")

        # ─── #2 종료 자본 ─────────────────────
        o2 = self.output_2_ending_capital
        lines.append("## #2 종료 자본 (Ending Capital)")
        lines.append("")
        lines.append(f"- **현금 (Cash)**: {_fmt_krw(o2.get('cash_krw'))}")
        lines.append(f"- **포지션 시가 (Position MV)**: {_fmt_krw(o2.get('position_market_value_krw'))}")
        lines.append(f"- **총 자본 (Total Equity)**: {_fmt_krw(o2.get('total_equity_krw'))}")
        lines.append(f"- **총 수익률 (Return)**: {_fmt_pct(o2.get('total_return_pct'))}")
        lines.append("")

        # ─── #3 실현 P&L ──────────────────────
        lines.append("## #3 실현 P&L (Realized P&L)")
        lines.append("")
        lines.append(f"- **금액**: {_fmt_pnl(self.output_3_realized_pnl.get('amount_krw'))}")
        lines.append("")

        # ─── #4 미실현 P&L ────────────────────
        lines.append("## #4 미실현 P&L (Unrealized P&L)")
        lines.append("")
        lines.append(f"- **금액**: {_fmt_pnl(self.output_4_unrealized_pnl.get('amount_krw'))}")
        lines.append("")

        # ─── #5 수수료/슬리피지 ──────────────
        o5 = self.output_5_fees_slippage
        lines.append("## #5 수수료/세금/슬리피지 (Fees/Taxes/Slippage)")
        lines.append("")
        lines.append(f"- **수수료 (Fees)**: {_fmt_krw(o5.get('total_fees_krw'))}")
        lines.append(f"- **세금 (Taxes)**: {_fmt_krw(o5.get('total_taxes_krw'))}")
        lines.append(f"- **슬리피지 (Slippage)**: {_fmt_pnl(o5.get('total_slippage_krw'))}")
        lines.append(f"- **체결 건수 (Fill Count)**: {o5.get('fill_count', 0):,}")
        lines.append("")

        # ─── #6 전략 기여도 ───────────────────
        lines.append("## #6 전략 기여도 (Strategy Attribution)")
        lines.append("")
        attr6 = self.output_6_strategy_attribution
        if attr6:
            lines.append("| 전략 (Strategy) | 실현 P&L (Realized) |")
            lines.append("|---|---|")
            for r in attr6:
                lines.append(f"| {r.get('strategy_id', '?')} | {_fmt_pnl(r.get('realized_pnl_krw'))} |")
        else:
            lines.append("_데이터 없음 (No data)_")
        lines.append("")

        # ─── #7 종목 기여도 ───────────────────
        o7 = self.output_7_symbol_attribution
        lines.append("## #7 종목 기여도 (Symbol Attribution)")
        lines.append("")
        sym_list = o7.get("positions", [])
        if sym_list:
            lines.append("| 종목 | 수량 | 평단가 | 현재가 | 시가총액 | 미실현 P&L | % |")
            lines.append("|---|---|---|---|---|---|---|")
            for r in sym_list:
                lines.append(
                    f"| {r.get('symbol')} | {r.get('quantity')} | "
                    f"{_fmt_krw(r.get('avg_cost_krw'))} | {_fmt_krw(r.get('mark_price_krw'))} | "
                    f"{_fmt_krw(r.get('market_value_krw'))} | "
                    f"{_fmt_pnl(r.get('unrealized_pnl_krw'))} | "
                    f"{r.get('unrealized_pnl_pct', '0')}% |"
                )
        else:
            lines.append("_보유 포지션 없음 (No open positions)_")
        lines.append("")

        # ─── #8 거부 주문 ─────────────────────
        o8 = self.output_8_rejected_orders
        lines.append("## #8 거부된 주문 (Rejected Orders)")
        lines.append("")
        lines.append(f"- **총 평가 (Total Eval)**: {o8.get('total_evaluations', 0):,}")
        lines.append(f"- **승인 (Approved)**: {o8.get('approved', 0):,}")
        lines.append(f"- **거부 (Rejected)**: {o8.get('rejected', 0):,}")
        lines.append(f"- **거부율 (Rate)**: {_fmt_pct(o8.get('rejection_rate'))}")
        bg = o8.get("by_gate", {})
        if bg:
            lines.append("")
            lines.append("**게이트별 (By Gate)**:")
            for g, c in sorted(bg.items(), key=lambda x: -x[1]):
                lines.append(f"- {g}: {c:,}")
        lines.append("")

        # ─── #9 리스크 한도 사용 ──────────────
        o9 = self.output_9_risk_limit_usage
        lines.append("## #9 리스크 한도 사용 (Risk-Limit Usage)")
        lines.append("")
        lines.append(f"- **포트폴리오 경고 (Warnings)**: {o9.get('portfolio_warning_count', 0)}")
        warnings = o9.get("warnings", [])
        if warnings:
            for w in warnings:
                lines.append(f"  - ⚠️ {w}")
        lines.append("")

        # ─── #10 정합성 ───────────────────────
        o10 = self.output_10_reconciliation_status
        lines.append("## #10 정합성 (Reconciliation Status)")
        lines.append("")
        lines.append(f"- **상태 (Severity)**: {o10.get('severity', 'unknown')}")
        lines.append(f"- **불일치 (Mismatches)**: {o10.get('mismatch_count', 0)}")
        if o10.get("note"):
            lines.append(f"- **비고**: {o10['note']}")
        lines.append("")

        # ─── #11 예외 ─────────────────────────
        excs = self.output_11_exceptions
        lines.append("## #11 예외 (Exceptions)")
        lines.append("")
        if excs:
            lines.append(f"**총 {len(excs)}건**:")
            lines.append("")
            for e in excs[:20]:
                src = e.get("source", "?")
                msg = e.get("message", "")
                lines.append(f"- [{src}] {msg}")
            if len(excs) > 20:
                lines.append(f"- _... 외 {len(excs) - 20}건_")
        else:
            lines.append("_예외 없음 (No exceptions)_")
        lines.append("")

        # ─── #12 다음 세션 자본 ───────────────
        o12 = self.output_12_next_session_capacity
        lines.append("## #12 다음 세션 자본 추천 (Next Session Capacity)")
        lines.append("")
        lines.append(f"- **현재 (Current)**: {_fmt_krw(o12.get('current_capital_krw'))}")
        lines.append(f"- **추천 (Recommended)**: {_fmt_krw(o12.get('recommended_capital_krw'))}")
        lines.append(f"- **배수 (Multiplier)**: {o12.get('multiplier', '1.00')}x")
        lines.append(f"- **단계 (Stage)**: `{o12.get('stage', '?')}`")
        lines.append(f"- **리스크 신호 (Risk Signals)**: {o12.get('risk_signals', 0)}")
        sd = o12.get("signal_details", [])
        if sd:
            lines.append("")
            lines.append("**신호 상세 (Signal Details)**:")
            for s in sd:
                lines.append(f"- 🚨 {s}")
        lines.append("")
        lines.append(f"**근거 (Reasoning)**: {o12.get('reasoning', '')}")
        lines.append("")
        lines.append("---")
        lines.append(f"_Generated by JCPR Trading System Task 49 v{m.get('report_version', '?')}_")
        lines.append("")
        return "\n".join(lines)

    # ─── HTML 직렬화 ──────────────────────────
    def to_html(self) -> str:
        """HTML 문자열 — 인쇄 친화적 CSS."""
        md_text = self.to_markdown()
        # 단순 변환: <pre>로 감싸 monospace 보존 + 헤더만 처리
        # 본격 MD→HTML 변환은 외부 의존성이므로 여기서는 단순화
        body = _markdown_to_html(md_text)
        m = self.metadata
        title = f"일일 리포트 (Daily Report) — {m.get('session_date_kst', '?')}"
        return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>{escape(title)}</title>
<style>
    body {{
        font-family: -apple-system, "Apple SD Gothic Neo", "Malgun Gothic", sans-serif;
        max-width: 900px;
        margin: 2em auto;
        padding: 0 1em;
        line-height: 1.6;
        color: #222;
    }}
    h1 {{ border-bottom: 2px solid #333; padding-bottom: 0.3em; }}
    h2 {{ border-bottom: 1px solid #ccc; padding-bottom: 0.2em; margin-top: 2em; }}
    table {{ border-collapse: collapse; width: 100%; margin: 1em 0; }}
    th, td {{ border: 1px solid #ddd; padding: 0.4em 0.8em; text-align: left; }}
    th {{ background: #f5f5f5; }}
    code {{ background: #f0f0f0; padding: 0.1em 0.3em; border-radius: 3px; }}
    .signal {{ color: #c0392b; }}
    @media print {{
        body {{ max-width: 100%; margin: 1em; }}
        h2 {{ page-break-after: avoid; }}
        table {{ page-break-inside: avoid; }}
    }}
</style>
</head>
<body>
{body}
</body>
</html>
"""


# ─────────────────────────────────────────────────
# 헬퍼 (Helpers)
# ─────────────────────────────────────────────────

def _json_default(o):
    """JSON encoder fallback for Decimal/date/datetime."""
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    if isinstance(o, Path):
        return str(o)
    raise TypeError(f"Type {type(o).__name__} not serializable")


def _fmt_krw(v: Any) -> str:
    """KRW 콤마 포맷."""
    if v is None or v == "":
        return "N/A"
    try:
        d = Decimal(str(v))
        sign = "-" if d < 0 else ""
        return f"{sign}{int(abs(d)):,} KRW"
    except Exception:  # noqa: BLE001
        return str(v)


def _fmt_pnl(v: Any) -> str:
    """P&L 부호 강조."""
    if v is None or v == "":
        return "N/A"
    try:
        d = Decimal(str(v))
        sign = "+" if d >= 0 else ""
        return f"{sign}{_fmt_krw(d)}"
    except Exception:  # noqa: BLE001
        return str(v)


def _fmt_pct(v: Any) -> str:
    """퍼센트 — 입력은 이미 % 단위 (예: 5.0 = 5%)."""
    if v is None or v == "":
        return "N/A"
    try:
        d = Decimal(str(v))
        return f"{d:.2f}%"
    except Exception:  # noqa: BLE001
        return str(v)


def _markdown_to_html(md: str) -> str:
    """매우 단순한 MD → HTML 변환 (외부 의존성 회피)."""
    lines = md.split("\n")
    out: list[str] = []
    in_table = False
    table_buf: list[str] = []

    def flush_table():
        nonlocal in_table, table_buf
        if not table_buf:
            return
        rows = [r.strip() for r in table_buf if r.strip()]
        # rows[0] = header, rows[1] = separator, rest = data
        if len(rows) < 2:
            for r in rows:
                out.append(f"<p>{escape(r)}</p>")
            table_buf = []
            in_table = False
            return
        out.append("<table>")
        # header
        h_cells = [c.strip() for c in rows[0].strip("|").split("|")]
        out.append("<tr>" + "".join(f"<th>{escape(c)}</th>" for c in h_cells) + "</tr>")
        # data
        for r in rows[2:]:
            cells = [c.strip() for c in r.strip("|").split("|")]
            out.append("<tr>" + "".join(f"<td>{escape(c)}</td>" for c in cells) + "</tr>")
        out.append("</table>")
        table_buf = []
        in_table = False

    for ln in lines:
        stripped = ln.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            in_table = True
            table_buf.append(ln)
            continue
        else:
            if in_table:
                flush_table()

        if stripped.startswith("# "):
            out.append(f"<h1>{escape(stripped[2:])}</h1>")
        elif stripped.startswith("## "):
            out.append(f"<h2>{escape(stripped[3:])}</h2>")
        elif stripped.startswith("### "):
            out.append(f"<h3>{escape(stripped[4:])}</h3>")
        elif stripped == "---":
            out.append("<hr>")
        elif stripped.startswith("- "):
            content = stripped[2:]
            # bold ** ** 처리
            content = _bold(content)
            out.append(f"<li>{content}</li>")
        elif stripped == "":
            out.append("")
        else:
            content = _bold(escape(stripped))
            if content:
                out.append(f"<p>{content}</p>")

    if in_table:
        flush_table()

    # <li> 들을 <ul>로 묶기
    result: list[str] = []
    in_ul = False
    for ln in out:
        if ln.startswith("<li>"):
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


def _bold(text: str) -> str:
    """**bold** 처리 — 단순."""
    parts = text.split("**")
    if len(parts) < 3:
        return text
    out = []
    for i, p in enumerate(parts):
        if i % 2 == 1:
            out.append(f"<strong>{p}</strong>")
        else:
            out.append(p)
    return "".join(out)
