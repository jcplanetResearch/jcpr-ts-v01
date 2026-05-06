"""
거부 분석기 (Rejection Analyzer)
==================================

JCPR Trading System - jcpr-ts-v01
Task 20 v0.1

Task 19 risk_decisions.jsonl audit log → RejectionReport.

원칙:
- Read-only — JSONL 변경 없음
- 비밀 키워드 자동 무시 (secret/token/password/app_key/account_no)
- 깨진 JSONL 라인 skip (resilient)
- UTC tz-aware 입력, KST 시간대 분석
- 빈 데이터 graceful handling
"""

from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from .rejection_diagnostics import diagnose
from .rejection_report import (
    GateRejectionAnalysis,
    RejectionReport,
)

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")

REPORT_VERSION = "0.1"

# 비밀 누출 방지
_SECRET_KEYWORDS = ("secret", "token", "password", "app_key", "account_no")


def _is_secret_key(key: str) -> bool:
    return any(kw in key.lower() for kw in _SECRET_KEYWORDS)


def _parse_iso(s: Any) -> Optional[datetime]:
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _within_window(
    dt: Optional[datetime],
    since_utc: Optional[datetime],
    until_utc: Optional[datetime],
) -> bool:
    if dt is None:
        return True
    if since_utc is not None and dt < since_utc:
        return False
    if until_utc is not None and dt > until_utc:
        return False
    return True


def _read_jsonl_filtered(
    path: Path,
    *,
    since_utc: Optional[datetime] = None,
    until_utc: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """JSONL 파일 → 시간 필터된 dict 리스트."""
    if not path.exists():
        logger.info("audit log 없음: %s", path)
        return []

    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                logger.warning(
                    "audit log 라인 %d 파싱 실패: %s — %s",
                    line_no, path.name, e,
                )
                continue
            if not isinstance(rec, dict):
                continue
            ts = _parse_iso(rec.get("decided_at_utc"))
            if not _within_window(ts, since_utc, until_utc):
                continue
            rows.append(rec)
    return rows


# ─────────────────────────────────────────────────
# Analyzer
# ─────────────────────────────────────────────────

class RejectionAnalyzer:
    """
    Task 19 risk audit log을 분석하여 RejectionReport 생성.

    Args:
        window_minutes: 추세 윈도우 크기 (기본 30분)
        thresholds: 진단 임계값 override (None=기본)
    """

    def __init__(
        self,
        *,
        window_minutes: int = 30,
        thresholds: Optional[dict[str, float]] = None,
    ):
        if window_minutes <= 0:
            raise ValueError(f"window_minutes 양수 필요: {window_minutes}")
        self._window_min = window_minutes
        self._thresholds = thresholds

    # ------------------------------------------------------------------
    # 메인
    # ------------------------------------------------------------------

    def analyze(
        self,
        audit_path: str | Path,
        *,
        since_utc: Optional[datetime] = None,
        until_utc: Optional[datetime] = None,
    ) -> RejectionReport:
        """
        risk_decisions.jsonl → RejectionReport.
        """
        # 시간 검증
        if since_utc is not None and since_utc.tzinfo is None:
            raise ValueError("since_utc tz-aware 필수")
        if until_utc is not None and until_utc.tzinfo is None:
            raise ValueError("until_utc tz-aware 필수")

        path = Path(audit_path)
        rows = _read_jsonl_filtered(
            path, since_utc=since_utc, until_utc=until_utc,
        )

        # 메타데이터
        metadata = {
            "source_path": str(path),
            "since_utc": since_utc.isoformat() if since_utc else None,
            "until_utc": until_utc.isoformat() if until_utc else None,
            "window_minutes": self._window_min,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "report_version": REPORT_VERSION,
            "system": "jcpr-ts-v01",
        }

        if not rows:
            return RejectionReport(
                metadata=metadata,
                total_evaluations=0,
                pass_count=0,
                reject_count=0,
                rejection_rate=0.0,
            )

        # 집계
        return self._analyze_rows(rows, metadata)

    # ------------------------------------------------------------------
    # 내부 — 집계 로직
    # ------------------------------------------------------------------

    def _analyze_rows(
        self, rows: list[dict[str, Any]], metadata: dict[str, Any],
    ) -> RejectionReport:
        # 기본 카운트
        pass_count = 0
        reject_count = 0

        # 차원별
        by_gate_count: Counter[str] = Counter()
        by_symbol_count: Counter[str] = Counter()
        by_strategy_count: Counter[str] = Counter()

        # 게이트별 상세
        gate_to_symbols: dict[str, Counter[str]] = defaultdict(Counter)
        gate_to_reasons: dict[str, Counter[str]] = defaultdict(Counter)
        gate_to_first_seen: dict[str, datetime] = {}
        gate_to_last_seen: dict[str, datetime] = {}

        # 매트릭스
        symbol_gate: dict[str, Counter[str]] = defaultdict(Counter)

        # 시간대별 (hour 0~23 KST)
        hour_pass: Counter[int] = Counter()
        hour_reject: Counter[int] = Counter()

        # 시계열 — 윈도우별로 집계 (나중에 정렬)
        # key = window_start_utc (정수 분 단위로 정렬된 datetime)
        window_records: dict[datetime, dict[str, int]] = defaultdict(
            lambda: {"pass": 0, "reject": 0}
        )

        for rec in rows:
            decision = rec.get("decision") or rec.get("outcome")
            ts = _parse_iso(rec.get("decided_at_utc"))

            if decision == "pass":
                pass_count += 1
                if ts is not None:
                    kst_dt = ts.astimezone(KST)
                    hour_pass[kst_dt.hour] += 1
                    win = self._floor_window(ts)
                    window_records[win]["pass"] += 1
                continue

            if decision != "reject":
                continue

            reject_count += 1

            # 거부 게이트 — 다양한 필드 이름 시도
            gate_name = (
                rec.get("rejected_by_gate")
                or rec.get("first_reject_gate")
                or rec.get("gate_name")
                or "unknown"
            )
            gate_name = str(gate_name)
            by_gate_count[gate_name] += 1

            # 종목 / 전략
            symbol = rec.get("symbol")
            if symbol:
                symbol = str(symbol)
                by_symbol_count[symbol] += 1
                symbol_gate[symbol][gate_name] += 1
                gate_to_symbols[gate_name][symbol] += 1

            strategy = rec.get("strategy_id") or rec.get("strategy")
            if strategy:
                by_strategy_count[str(strategy)] += 1

            # 사유
            reason = rec.get("reason") or rec.get("first_reject_reason")
            if reason:
                gate_to_reasons[gate_name][str(reason)] += 1

            # 시각 추적
            if ts is not None:
                if (
                    gate_name not in gate_to_first_seen
                    or ts < gate_to_first_seen[gate_name]
                ):
                    gate_to_first_seen[gate_name] = ts
                if (
                    gate_name not in gate_to_last_seen
                    or ts > gate_to_last_seen[gate_name]
                ):
                    gate_to_last_seen[gate_name] = ts

                # 시간대
                kst_dt = ts.astimezone(KST)
                hour_reject[kst_dt.hour] += 1

                # 윈도우
                win = self._floor_window(ts)
                window_records[win]["reject"] += 1

        total = pass_count + reject_count
        rate = (reject_count / total) if total > 0 else 0.0

        # 게이트별 분석
        by_gate: dict[str, GateRejectionAnalysis] = {}
        for name, count in by_gate_count.items():
            top_syms = gate_to_symbols[name].most_common(5)
            top_reasons = gate_to_reasons[name].most_common(5)
            by_gate[name] = GateRejectionAnalysis(
                gate_name=name,
                reject_count=count,
                rate_in_total=count / total if total > 0 else 0.0,
                top_symbols=[(s, c) for s, c in top_syms],
                top_reasons=[(r, c) for r, c in top_reasons],
                first_seen_utc=gate_to_first_seen.get(name),
                last_seen_utc=gate_to_last_seen.get(name),
            )

        # 시간대별
        by_hour_kst: dict[int, dict[str, Any]] = {}
        all_hours = sorted(set(hour_pass.keys()) | set(hour_reject.keys()))
        for h in all_hours:
            p = hour_pass.get(h, 0)
            r = hour_reject.get(h, 0)
            tot = p + r
            by_hour_kst[h] = {
                "pass_count": p,
                "reject_count": r,
                "total": tot,
                "rate": (r / tot) if tot > 0 else 0.0,
            }

        # 시계열 — 윈도우 정렬
        rolling: list[dict[str, Any]] = []
        for win_start in sorted(window_records.keys()):
            d = window_records[win_start]
            tot = d["pass"] + d["reject"]
            rolling.append({
                "window_start_utc": win_start.isoformat(),
                "window_start_kst": win_start.astimezone(KST).strftime(
                    "%Y-%m-%d %H:%M"
                ),
                "pass_count": d["pass"],
                "reject_count": d["reject"],
                "count": tot,
                "rate": (d["reject"] / tot) if tot > 0 else 0.0,
            })

        # 진단
        findings = diagnose(
            total_evaluations=total,
            reject_count=reject_count,
            by_gate_reject=dict(by_gate_count),
            by_symbol_reject=dict(by_symbol_count),
            rolling_rates=rolling,
            thresholds=self._thresholds,
        )

        # 매트릭스
        symbol_gate_matrix = {
            sym: dict(gates) for sym, gates in symbol_gate.items()
        }

        return RejectionReport(
            metadata=metadata,
            total_evaluations=total,
            pass_count=pass_count,
            reject_count=reject_count,
            rejection_rate=rate,
            by_gate=by_gate,
            by_symbol=dict(by_symbol_count),
            by_strategy=dict(by_strategy_count),
            by_hour_kst=by_hour_kst,
            symbol_gate_matrix=symbol_gate_matrix,
            rolling_rejection_rates=rolling,
            diagnostic_findings=findings,
        )

    # ------------------------------------------------------------------
    # save
    # ------------------------------------------------------------------

    def analyze_and_save(
        self,
        audit_path: str | Path,
        output_dir: str | Path,
        *,
        since_utc: Optional[datetime] = None,
        until_utc: Optional[datetime] = None,
        formats: tuple[str, ...] = ("json", "md", "html", "csv"),
        filename_prefix: str = "rejection_report",
    ) -> dict[str, Path]:
        """analyze + 파일 저장."""
        report = self.analyze(audit_path, since_utc=since_utc, until_utc=until_utc)
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        # 파일명 기반: since 또는 generated_at
        if since_utc is not None:
            label = since_utc.astimezone(KST).strftime("%Y-%m-%d")
        else:
            label = datetime.now(KST).strftime("%Y-%m-%d_%H%M")

        base = f"{filename_prefix}_{label}"
        result: dict[str, Path] = {}

        if "json" in formats:
            result["json"] = report.save_json(out_dir / f"{base}.json")
        if "md" in formats or "markdown" in formats:
            result["md"] = report.save_markdown(out_dir / f"{base}.md")
        if "html" in formats:
            result["html"] = report.save_html(out_dir / f"{base}.html")
        if "csv" in formats:
            result["csv"] = report.save_csv(out_dir / f"{base}_gates.csv")

        return result

    # ------------------------------------------------------------------
    # 윈도우 계산 헬퍼
    # ------------------------------------------------------------------

    def _floor_window(self, dt: datetime) -> datetime:
        """dt를 가장 가까운 윈도우 시작 시각으로 내림."""
        # UTC 기준으로 윈도우 정렬 (KST 변환은 표시용만)
        utc_dt = dt.astimezone(timezone.utc)
        # 오전 0시(UTC) 기준 분 단위
        epoch_min = int(utc_dt.timestamp() // 60)
        floored_min = (epoch_min // self._window_min) * self._window_min
        floored_ts = floored_min * 60
        return datetime.fromtimestamp(floored_ts, tz=timezone.utc)
