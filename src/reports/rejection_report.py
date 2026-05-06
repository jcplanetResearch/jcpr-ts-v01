"""
거부 분석 리포트 (Rejection Analysis Report)
==============================================

JCPR Trading System - jcpr-ts-v01
Task 20 v0.1

리포트 데이터 모델 + JSON / Markdown / HTML / CSV 출력.

원칙:
- frozen=True (immutable)
- Decimal 미사용 (count + float rate만)
- UTC tz-aware datetime
- KST 표시 변환 (시간대 분석)
- XSS escape (HTML)
"""

from __future__ import annotations

import csv
import html
import io
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from .rejection_diagnostics import DiagnosticFinding

KST = ZoneInfo("Asia/Seoul")


def _json_default(o: Any) -> Any:
    if isinstance(o, datetime):
        return o.isoformat()
    raise TypeError(f"JSON 직렬화 불가: {type(o)}")


# ─────────────────────────────────────────────────
# 게이트별 분석
# ─────────────────────────────────────────────────

@dataclass(frozen=True)
class GateRejectionAnalysis:
    """단일 게이트의 거부 분석."""
    gate_name: str
    reject_count: int
    rate_in_total: float                     # reject_count / total_evaluations
    top_symbols: list[tuple[str, int]] = field(default_factory=list)
    top_reasons: list[tuple[str, int]] = field(default_factory=list)
    first_seen_utc: Optional[datetime] = None
    last_seen_utc: Optional[datetime] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate_name": self.gate_name,
            "reject_count": self.reject_count,
            "rate_in_total": round(self.rate_in_total, 4),
            "top_symbols": [list(t) for t in self.top_symbols],
            "top_reasons": [list(t) for t in self.top_reasons],
            "first_seen_utc": (
                self.first_seen_utc.isoformat() if self.first_seen_utc else None
            ),
            "last_seen_utc": (
                self.last_seen_utc.isoformat() if self.last_seen_utc else None
            ),
        }


# ─────────────────────────────────────────────────
# 메인 리포트
# ─────────────────────────────────────────────────

@dataclass(frozen=True)
class RejectionReport:
    """리스크 거부 상세 분석 리포트."""
    metadata: dict[str, Any] = field(default_factory=dict)

    # 기본 통계
    total_evaluations: int = 0
    pass_count: int = 0
    reject_count: int = 0
    rejection_rate: float = 0.0

    # 차원별 분석
    by_gate: dict[str, GateRejectionAnalysis] = field(default_factory=dict)
    by_symbol: dict[str, int] = field(default_factory=dict)
    by_strategy: dict[str, int] = field(default_factory=dict)
    by_hour_kst: dict[int, dict[str, Any]] = field(default_factory=dict)

    # 매트릭스
    symbol_gate_matrix: dict[str, dict[str, int]] = field(default_factory=dict)

    # 시계열
    rolling_rejection_rates: list[dict[str, Any]] = field(default_factory=list)

    # 진단
    diagnostic_findings: list[DiagnosticFinding] = field(default_factory=list)

    # ------------------------------------------------------------------
    # 헬퍼
    # ------------------------------------------------------------------

    def has_critical_findings(self) -> bool:
        return any(f.severity == "critical" for f in self.diagnostic_findings)

    def has_warnings(self) -> bool:
        return any(
            f.severity in ("warning", "critical")
            for f in self.diagnostic_findings
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "metadata": dict(self.metadata),
            "summary": {
                "total_evaluations": self.total_evaluations,
                "pass_count": self.pass_count,
                "reject_count": self.reject_count,
                "rejection_rate": round(self.rejection_rate, 4),
            },
            "by_gate": {
                k: v.to_dict() for k, v in self.by_gate.items()
            },
            "by_symbol": dict(self.by_symbol),
            "by_strategy": dict(self.by_strategy),
            "by_hour_kst": dict(self.by_hour_kst),
            "symbol_gate_matrix": {
                sym: dict(gates) for sym, gates in self.symbol_gate_matrix.items()
            },
            "rolling_rejection_rates": list(self.rolling_rejection_rates),
            "diagnostic_findings": [f.to_dict() for f in self.diagnostic_findings],
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

        # 헤더
        lines.append("# 리스크 거부 분석 리포트 (Risk Rejection Report)")
        lines.append("")
        lines.append(f"**소스 (Source)**: `{m.get('source_path', 'unknown')}`")
        if m.get("since_utc"):
            lines.append(f"**기간 시작 (Since)**: {m.get('since_utc')}")
        if m.get("until_utc"):
            lines.append(f"**기간 종료 (Until)**: {m.get('until_utc')}")
        lines.append(f"**윈도우 크기**: {m.get('window_minutes', 30)}분")
        lines.append(f"**생성 시각**: {m.get('generated_at_utc', '?')}")
        lines.append(f"**시스템**: jcpr-ts-v01 — Task 20 v{m.get('report_version', '0.1')}")
        lines.append("")
        lines.append("---")
        lines.append("")

        # 요약
        lines.append("## 1. 요약 (Summary)")
        lines.append("")
        lines.append(f"- **전체 평가 (Total)**: {self.total_evaluations:,}건")
        lines.append(f"- **통과 (Pass)**: {self.pass_count:,}건")
        lines.append(f"- **거부 (Reject)**: {self.reject_count:,}건")
        lines.append(f"- **거부율 (Rejection Rate)**: {self.rejection_rate:.2%}")
        lines.append("")

        # 진단 (먼저 — 운영자 주목 필요)
        lines.append("## 2. 자동 진단 (Auto Diagnostics)")
        lines.append("")
        if self.diagnostic_findings:
            for f in self.diagnostic_findings:
                emoji = {
                    "critical": "🔴",
                    "warning": "⚠️",
                    "info": "ℹ️",
                }.get(f.severity, "•")
                lines.append(f"### {emoji} `{f.code}` ({f.severity})")
                lines.append("")
                lines.append(f"**문제**: {f.message}")
                lines.append("")
                lines.append(f"**권장 조치**: {f.recommendation}")
                if f.related_gate:
                    lines.append(f"")
                    lines.append(f"_관련 게이트: `{f.related_gate}`_")
                if f.related_symbol:
                    lines.append(f"_관련 종목: `{f.related_symbol}`_")
                lines.append("")
        else:
            lines.append("진단 데이터 없음")
        lines.append("")

        # 게이트별 분석
        lines.append("## 3. 게이트별 분석 (By Gate)")
        lines.append("")
        if self.by_gate:
            lines.append("| 게이트 | 거부 횟수 | 전체 대비 | 최다 거부 종목 |")
            lines.append("|---|---:|---:|---|")
            sorted_gates = sorted(
                self.by_gate.items(),
                key=lambda x: -x[1].reject_count,
            )
            for name, gate in sorted_gates:
                top_sym_str = ", ".join(
                    f"`{s}` ({c})" for s, c in gate.top_symbols[:3]
                ) if gate.top_symbols else "-"
                lines.append(
                    f"| `{name}` | {gate.reject_count:,} "
                    f"| {gate.rate_in_total:.2%} "
                    f"| {top_sym_str} |"
                )
            lines.append("")

            # 게이트별 상세 (사유 top 3)
            for name, gate in sorted_gates[:5]:  # 상위 5개만 상세
                if gate.top_reasons:
                    lines.append(f"**`{name}` 주요 사유:**")
                    for reason, cnt in gate.top_reasons[:3]:
                        # 사유는 길 수 있어 간단히 자름
                        short_reason = reason[:120]
                        lines.append(f"  - {cnt}건: {short_reason}{'...' if len(reason) > 120 else ''}")
                    lines.append("")
        else:
            lines.append("게이트 데이터 없음")
        lines.append("")

        # 종목별
        lines.append("## 4. 종목별 거부 (By Symbol)")
        lines.append("")
        if self.by_symbol:
            sorted_syms = sorted(self.by_symbol.items(), key=lambda x: -x[1])[:15]
            lines.append("| 종목 | 거부 횟수 | 전체 거부 대비 |")
            lines.append("|---|---:|---:|")
            for sym, cnt in sorted_syms:
                pct = (cnt / self.reject_count) if self.reject_count > 0 else 0.0
                lines.append(f"| `{sym}` | {cnt:,} | {pct:.2%} |")
        else:
            lines.append("종목 데이터 없음")
        lines.append("")

        # 시간대별
        lines.append("## 5. 시간대별 분포 (By Hour, KST)")
        lines.append("")
        if self.by_hour_kst:
            lines.append("| 시간 (KST) | 평가 | 통과 | 거부 | 거부율 |")
            lines.append("|---|---:|---:|---:|---:|")
            for h in sorted(self.by_hour_kst.keys()):
                d = self.by_hour_kst[h]
                rate = d.get("rate", 0.0)
                lines.append(
                    f"| {h:02d}:00~{h:02d}:59 "
                    f"| {d.get('total', 0):,} "
                    f"| {d.get('pass_count', 0):,} "
                    f"| {d.get('reject_count', 0):,} "
                    f"| {rate:.2%} |"
                )
        else:
            lines.append("시간대 데이터 없음")
        lines.append("")

        # 30분 윈도우 추세
        lines.append("## 6. 추세 (Rolling Window)")
        lines.append("")
        if self.rolling_rejection_rates:
            window_min = m.get("window_minutes", 30)
            lines.append(f"_{window_min}분 윈도우 단위 거부율 추세_")
            lines.append("")
            lines.append("| 윈도우 시작 (KST) | 평가 | 거부 | 거부율 |")
            lines.append("|---|---:|---:|---:|")
            for w in self.rolling_rejection_rates:
                lines.append(
                    f"| {w.get('window_start_kst', '?')} "
                    f"| {w.get('count', 0):,} "
                    f"| {w.get('reject_count', 0):,} "
                    f"| {w.get('rate', 0.0):.2%} |"
                )
        else:
            lines.append("추세 데이터 없음")
        lines.append("")

        # 종목 × 게이트 매트릭스 (상위만)
        lines.append("## 7. 종목 × 게이트 매트릭스 (Symbol × Gate)")
        lines.append("")
        if self.symbol_gate_matrix:
            # 상위 종목 5개만
            sorted_syms = sorted(
                self.symbol_gate_matrix.items(),
                key=lambda x: -sum(x[1].values()),
            )[:5]
            # 모든 게이트 수집
            all_gates: set[str] = set()
            for _, gates in sorted_syms:
                all_gates.update(gates.keys())
            sorted_gates = sorted(all_gates)

            if sorted_gates:
                header = "| 종목 | " + " | ".join(f"`{g}`" for g in sorted_gates) + " |"
                lines.append(header)
                lines.append(
                    "|---|" + "|".join(["---:" for _ in sorted_gates]) + "|"
                )
                for sym, gates in sorted_syms:
                    row = f"| `{sym}` "
                    for g in sorted_gates:
                        row += f"| {gates.get(g, 0):,} "
                    row += "|"
                    lines.append(row)
            else:
                lines.append("데이터 없음")
        else:
            lines.append("매트릭스 데이터 없음")
        lines.append("")

        lines.append("---")
        lines.append("")
        lines.append(
            "_본 리포트는 자동 생성됨 — 운영자 검토 필수._"
        )
        lines.append(
            "_(Auto-generated — operator review required.)_"
        )

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # HTML
    # ------------------------------------------------------------------

    def to_html(self) -> str:
        """단일 파일 HTML — 외부 의존성 없음."""
        md = self.to_markdown()
        body = self._markdown_to_html_simple(md)

        m = self.metadata
        title = (
            f"Risk Rejection Report — "
            f"{html.escape(str(m.get('source_path', '')))}"
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
    line-height: 1.6; max-width: 1100px; margin: 2em auto; padding: 1em;
    color: #222; background: #fafafa;
  }}
  h1 {{ border-bottom: 3px solid #c53030; padding-bottom: 0.3em; color: #c53030; }}
  h2 {{ border-bottom: 1px solid #e2e8f0; padding-bottom: 0.2em; margin-top: 2em; color: #2d3748; }}
  h3 {{ margin-top: 1.5em; color: #2d3748; }}
  table {{ border-collapse: collapse; width: 100%; margin: 1em 0; }}
  th, td {{ border: 1px solid #cbd5e0; padding: 6px 10px; text-align: left; }}
  th {{ background: #edf2f7; }}
  td {{ background: white; }}
  td.numeric {{ text-align: right; }}
  code {{ background: #edf2f7; padding: 2px 6px; border-radius: 3px;
          font-family: "Menlo", "Consolas", monospace; font-size: 0.92em; }}
  ul, ol {{ margin-left: 1.4em; }}
  li {{ margin: 0.3em 0; }}
  hr {{ border: none; border-top: 1px solid #cbd5e0; margin: 2em 0; }}
  em {{ color: #4a5568; }}
  strong {{ color: #1a202c; }}
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
  Generated by jcpr-ts-v01 — Task 20 v0.1 — {html.escape(str(m.get('generated_at_utc', '')))}
</div>
</body>
</html>"""

    @staticmethod
    def _markdown_to_html_simple(md: str) -> str:
        """간단한 Markdown → HTML (Task 49 방식과 동일)."""
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
                    f"<{tag}>{RejectionReport._inline(c)}</{tag}>" for c in cells
                ) + "</tr>"
                out_lines.append(row)
                continue
            else:
                flush_table()

            if line.startswith("# "):
                out_lines.append(f"<h1>{RejectionReport._inline(line[2:])}</h1>")
            elif line.startswith("## "):
                out_lines.append(f"<h2>{RejectionReport._inline(line[3:])}</h2>")
            elif line.startswith("### "):
                out_lines.append(f"<h3>{RejectionReport._inline(line[4:])}</h3>")
            elif line == "---":
                out_lines.append("<hr>")
            elif line.startswith("  - "):
                out_lines.append(f"<li>{RejectionReport._inline(line[4:])}</li>")
            elif line.startswith("- "):
                out_lines.append(f"<li>{RejectionReport._inline(line[2:])}</li>")
            elif line == "":
                out_lines.append("")
            else:
                out_lines.append(f"<p>{RejectionReport._inline(line)}</p>")

        flush_table()

        # <li> 그룹화
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
        s = html.escape(text)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        s = re.sub(r"_([^_]+)_", r"<em>\1</em>", s)
        return s

    # ------------------------------------------------------------------
    # CSV (게이트별 요약)
    # ------------------------------------------------------------------

    def to_csv_summary(self) -> str:
        """게이트별 요약을 CSV로 — Excel/Sheets에서 분석."""
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "gate_name", "reject_count", "rate_in_total",
            "top_symbol", "top_symbol_count",
            "first_seen_utc", "last_seen_utc",
        ])
        sorted_gates = sorted(
            self.by_gate.values(),
            key=lambda g: -g.reject_count,
        )
        for gate in sorted_gates:
            top_sym = ""
            top_cnt = ""
            if gate.top_symbols:
                top_sym, top_cnt_int = gate.top_symbols[0]
                top_cnt = str(top_cnt_int)
            writer.writerow([
                gate.gate_name,
                gate.reject_count,
                f"{gate.rate_in_total:.4f}",
                top_sym,
                top_cnt,
                gate.first_seen_utc.isoformat() if gate.first_seen_utc else "",
                gate.last_seen_utc.isoformat() if gate.last_seen_utc else "",
            ])
        return buf.getvalue()

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

    def save_csv(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.to_csv_summary(), encoding="utf-8")
        return p
