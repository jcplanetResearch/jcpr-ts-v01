"""
감사 로그 인덱서 (Audit Indexer)
==================================

JCPR Trading System - jcpr-ts-v01
Task A3 v0.1 — Observability Infrastructure

JSONL 감사 로그 검색 + trace 재구성.
(Searches JSONL audit logs and reconstructs traces.)

설계 (Design):
    - 인덱싱 없이 sequential scan (단순, 신뢰성 우선)
    - 메모리 효율: streaming (제너레이터)
    - trace_id로 모든 관련 이벤트 조회
    - span_id 계층으로 트리 재구성
    - 시간 / 이벤트 타입 / origin 필터

용도 (Use cases):
    - "이 거래가 왜 일어났나?" 사후 재구성
    - 운영자 디버깅
    - 감사 / 규제 대응
    - Task 49 일일 리포트의 input_11_exceptions 보강

사용 (Usage):
    indexer = AuditIndexer(audit_dir="data/audit")
    
    # trace_id로 조회 — 전체 경로
    events = indexer.find_by_trace("trc-20260507-a1b2c3d4")
    tree = indexer.build_trace_tree("trc-20260507-a1b2c3d4")
    
    # 시간 범위 + 이벤트 타입 필터
    events = indexer.search(
        since_utc=datetime(2026, 5, 7, tzinfo=timezone.utc),
        event_types={"risk_evaluation", "exception"},
    )
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence


# ─────────────────────────────────────────────────
# 데이터 모델 (Data Models)
# ─────────────────────────────────────────────────

@dataclass(frozen=True)
class AuditEvent:
    """파싱된 audit 이벤트."""
    timestamp_utc: datetime
    event_type: str
    severity: str
    trace_id: str
    span_id: str
    parent_span_id: Optional[str]
    origin: str
    operator_id: Optional[str]
    session_id: str
    correlation_keys: dict[str, Any]
    payload: dict[str, Any]
    raw: dict[str, Any]   # 원본 (디버깅용)
    source_file: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp_utc": self.timestamp_utc.isoformat(),
            "event_type": self.event_type,
            "severity": self.severity,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "origin": self.origin,
            "operator_id": self.operator_id,
            "session_id": self.session_id,
            "correlation_keys": self.correlation_keys,
            "payload": self.payload,
            "source_file": self.source_file,
        }


@dataclass(frozen=True)
class SpanNode:
    """trace 트리의 노드."""
    span_id: str
    event: AuditEvent
    children: tuple["SpanNode", ...]

    def total_events(self) -> int:
        """자기 + 자손 카운트."""
        return 1 + sum(c.total_events() for c in self.children)

    def to_dict(self) -> dict[str, Any]:
        return {
            "span_id": self.span_id,
            "event": self.event.to_dict(),
            "children": [c.to_dict() for c in self.children],
        }


@dataclass(frozen=True)
class TraceSummary:
    """trace_id의 요약."""
    trace_id: str
    event_count: int
    span_count: int
    start_utc: datetime
    end_utc: datetime
    duration_ms: float
    origin: str
    session_id: str
    event_types: dict[str, int]
    severity_counts: dict[str, int]
    has_exceptions: bool
    has_critical: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "event_count": self.event_count,
            "span_count": self.span_count,
            "start_utc": self.start_utc.isoformat(),
            "end_utc": self.end_utc.isoformat(),
            "duration_ms": self.duration_ms,
            "origin": self.origin,
            "session_id": self.session_id,
            "event_types": self.event_types,
            "severity_counts": self.severity_counts,
            "has_exceptions": self.has_exceptions,
            "has_critical": self.has_critical,
        }


# ─────────────────────────────────────────────────
# 헬퍼 (Helpers)
# ─────────────────────────────────────────────────

# audit 파일 패턴
_AUDIT_FILE_PATTERN = re.compile(r"^audit(?:_(\d{8}))?\.jsonl$")


def _parse_event(raw: dict[str, Any], source_file: str) -> Optional[AuditEvent]:
    """raw dict → AuditEvent. 형식 오류 시 None."""
    try:
        ts_str = raw["timestamp_utc"]
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        trace = raw.get("trace", {})

        return AuditEvent(
            timestamp_utc=ts,
            event_type=raw.get("event_type", "unknown"),
            severity=raw.get("severity", "info"),
            trace_id=trace.get("trace_id", ""),
            span_id=trace.get("span_id", ""),
            parent_span_id=trace.get("parent_span_id"),
            origin=trace.get("origin", "unknown"),
            operator_id=trace.get("operator_id"),
            session_id=trace.get("session_id", ""),
            correlation_keys=trace.get("correlation_keys", {}),
            payload=raw.get("payload", {}),
            raw=raw,
            source_file=source_file,
        )
    except (KeyError, ValueError, TypeError):
        return None


# ─────────────────────────────────────────────────
# 인덱서 (Indexer)
# ─────────────────────────────────────────────────

@dataclass
class AuditIndexer:
    """
    감사 로그 검색기.

    Args:
        audit_dir: 감사 로그 디렉터리
        max_lines_per_file: 파일당 최대 라인 (안전 한도)
    """

    audit_dir: str
    max_lines_per_file: int = 1_000_000  # 100만 라인

    # ─────────────────────────────────────────
    # 파일 스캔 (File Discovery)
    # ─────────────────────────────────────────

    def list_files(
        self,
        *,
        since_date: Optional[str] = None,  # "YYYYMMDD"
        until_date: Optional[str] = None,
    ) -> list[Path]:
        """audit_*.jsonl 파일 목록 — 정렬됨 (오래된 것 먼저)."""
        d = Path(self.audit_dir)
        if not d.exists() or not d.is_dir():
            return []

        files: list[tuple[str, Path]] = []
        for p in d.iterdir():
            if not p.is_file():
                continue
            m = _AUDIT_FILE_PATTERN.match(p.name)
            if not m:
                continue
            date_part = m.group(1) or "00000000"
            # 필터
            if since_date and date_part < since_date:
                continue
            if until_date and date_part > until_date:
                continue
            files.append((date_part, p))

        files.sort(key=lambda x: x[0])
        return [p for _, p in files]

    # ─────────────────────────────────────────
    # 스트리밍 스캔 (Streaming Scan)
    # ─────────────────────────────────────────

    def _iter_events(
        self,
        files: Optional[Sequence[Path]] = None,
    ) -> Iterator[AuditEvent]:
        """모든 파일 순회 — 제너레이터."""
        target = files if files is not None else self.list_files()
        for f in target:
            try:
                with f.open("r", encoding="utf-8") as fh:
                    for i, line in enumerate(fh):
                        if i >= self.max_lines_per_file:
                            break
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            raw = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(raw, dict):
                            continue
                        ev = _parse_event(raw, source_file=str(f))
                        if ev is not None:
                            yield ev
            except OSError:
                continue

    # ─────────────────────────────────────────
    # 검색 API (Search)
    # ─────────────────────────────────────────

    def search(
        self,
        *,
        trace_id: Optional[str] = None,
        session_id: Optional[str] = None,
        origin: Optional[str] = None,
        event_types: Optional[set[str]] = None,
        severities: Optional[set[str]] = None,
        since_utc: Optional[datetime] = None,
        until_utc: Optional[datetime] = None,
        symbol: Optional[str] = None,
        strategy_id: Optional[str] = None,
        limit: int = 10000,
    ) -> list[AuditEvent]:
        """
        다중 조건 검색.

        Returns:
            AuditEvent 리스트 (시간순)
        """
        # 날짜로 파일 사전 필터
        since_date = since_utc.strftime("%Y%m%d") if since_utc else None
        until_date = until_utc.strftime("%Y%m%d") if until_utc else None
        files = self.list_files(since_date=since_date, until_date=until_date)

        results: list[AuditEvent] = []
        for ev in self._iter_events(files):
            # 시간 필터 (정밀)
            if since_utc and ev.timestamp_utc < since_utc:
                continue
            if until_utc and ev.timestamp_utc > until_utc:
                continue
            # 메타 필터
            if trace_id and ev.trace_id != trace_id:
                continue
            if session_id and ev.session_id != session_id:
                continue
            if origin and ev.origin != origin:
                continue
            if event_types and ev.event_type not in event_types:
                continue
            if severities and ev.severity not in severities:
                continue
            # correlation_keys 필터
            if symbol:
                ev_symbol = (
                    ev.correlation_keys.get("symbol")
                    or ev.payload.get("symbol")
                )
                if ev_symbol != symbol:
                    continue
            if strategy_id:
                ev_sid = (
                    ev.correlation_keys.get("strategy_id")
                    or ev.payload.get("strategy_id")
                )
                if ev_sid != strategy_id:
                    continue

            results.append(ev)
            if len(results) >= limit:
                break

        # 시간순 정렬
        results.sort(key=lambda e: e.timestamp_utc)
        return results

    # ─────────────────────────────────────────
    # trace_id 조회 (Trace Reconstruction)
    # ─────────────────────────────────────────

    def find_by_trace(self, trace_id: str) -> list[AuditEvent]:
        """trace_id의 모든 이벤트 — 시간순."""
        return self.search(trace_id=trace_id, limit=100000)

    def build_trace_tree(self, trace_id: str) -> Optional[SpanNode]:
        """
        trace_id의 span 계층 트리.

        Returns:
            루트 SpanNode (parent_span_id가 None인 첫 이벤트)
            없으면 None
        """
        events = self.find_by_trace(trace_id)
        if not events:
            return None

        # span_id → 이벤트 매핑 (첫 이벤트 사용)
        span_events: dict[str, AuditEvent] = {}
        for ev in events:
            if ev.span_id not in span_events:
                span_events[ev.span_id] = ev

        # parent → children 매핑
        children_map: dict[Optional[str], list[str]] = {}
        for span_id, ev in span_events.items():
            parent = ev.parent_span_id
            children_map.setdefault(parent, []).append(span_id)

        # 루트 찾기 (parent_span_id is None)
        roots = children_map.get(None, [])
        if not roots:
            # 데이터 손상 — 가장 오래된 이벤트를 루트로
            return None

        # 트리 빌드 (재귀)
        def build(span_id: str) -> SpanNode:
            ev = span_events[span_id]
            child_ids = children_map.get(span_id, [])
            # 시간순 정렬
            child_ids.sort(key=lambda sid: span_events[sid].timestamp_utc)
            return SpanNode(
                span_id=span_id,
                event=ev,
                children=tuple(build(cid) for cid in child_ids),
            )

        # 루트가 여러 개일 수 있음 (드물지만 — 가장 오래된 것)
        roots.sort(key=lambda sid: span_events[sid].timestamp_utc)
        return build(roots[0])

    def trace_summary(self, trace_id: str) -> Optional[TraceSummary]:
        """trace의 요약 (start/end/event_count 등)."""
        events = self.find_by_trace(trace_id)
        if not events:
            return None

        start = events[0].timestamp_utc
        end = events[-1].timestamp_utc
        duration_ms = (end - start).total_seconds() * 1000

        event_types: dict[str, int] = {}
        severity_counts: dict[str, int] = {}
        spans: set[str] = set()
        has_exception = False
        has_critical = False

        for ev in events:
            event_types[ev.event_type] = event_types.get(ev.event_type, 0) + 1
            severity_counts[ev.severity] = severity_counts.get(ev.severity, 0) + 1
            spans.add(ev.span_id)
            if ev.event_type == "exception":
                has_exception = True
            if ev.severity == "critical":
                has_critical = True

        return TraceSummary(
            trace_id=trace_id,
            event_count=len(events),
            span_count=len(spans),
            start_utc=start,
            end_utc=end,
            duration_ms=duration_ms,
            origin=events[0].origin,
            session_id=events[0].session_id,
            event_types=event_types,
            severity_counts=severity_counts,
            has_exceptions=has_exception,
            has_critical=has_critical,
        )

    # ─────────────────────────────────────────
    # 집계 (Aggregation)
    # ─────────────────────────────────────────

    def list_traces(
        self,
        *,
        session_id: Optional[str] = None,
        since_utc: Optional[datetime] = None,
        until_utc: Optional[datetime] = None,
        only_with_exceptions: bool = False,
        only_with_critical: bool = False,
        limit: int = 1000,
    ) -> list[TraceSummary]:
        """세션/시간 범위의 모든 trace 요약."""
        events = self.search(
            session_id=session_id,
            since_utc=since_utc,
            until_utc=until_utc,
            limit=100000,
        )

        # trace_id별 그룹핑
        by_trace: dict[str, list[AuditEvent]] = {}
        for ev in events:
            if ev.trace_id:
                by_trace.setdefault(ev.trace_id, []).append(ev)

        summaries: list[TraceSummary] = []
        for tid, evs in by_trace.items():
            if not evs:
                continue
            evs.sort(key=lambda e: e.timestamp_utc)
            start = evs[0].timestamp_utc
            end = evs[-1].timestamp_utc
            event_types: dict[str, int] = {}
            severity_counts: dict[str, int] = {}
            spans: set[str] = set()
            has_exc = False
            has_crit = False
            for ev in evs:
                event_types[ev.event_type] = event_types.get(ev.event_type, 0) + 1
                severity_counts[ev.severity] = severity_counts.get(ev.severity, 0) + 1
                spans.add(ev.span_id)
                if ev.event_type == "exception":
                    has_exc = True
                if ev.severity == "critical":
                    has_crit = True

            if only_with_exceptions and not has_exc:
                continue
            if only_with_critical and not has_crit:
                continue

            summaries.append(TraceSummary(
                trace_id=tid,
                event_count=len(evs),
                span_count=len(spans),
                start_utc=start,
                end_utc=end,
                duration_ms=(end - start).total_seconds() * 1000,
                origin=evs[0].origin,
                session_id=evs[0].session_id,
                event_types=event_types,
                severity_counts=severity_counts,
                has_exceptions=has_exc,
                has_critical=has_crit,
            ))

        summaries.sort(key=lambda s: s.start_utc, reverse=True)
        return summaries[:limit]

    def stats(
        self,
        *,
        since_utc: Optional[datetime] = None,
        until_utc: Optional[datetime] = None,
    ) -> dict[str, Any]:
        """기간 집계."""
        events = self.search(
            since_utc=since_utc, until_utc=until_utc, limit=1000000,
        )
        total = len(events)
        by_type: dict[str, int] = {}
        by_severity: dict[str, int] = {}
        by_origin: dict[str, int] = {}
        traces: set[str] = set()
        sessions: set[str] = set()
        for ev in events:
            by_type[ev.event_type] = by_type.get(ev.event_type, 0) + 1
            by_severity[ev.severity] = by_severity.get(ev.severity, 0) + 1
            by_origin[ev.origin] = by_origin.get(ev.origin, 0) + 1
            if ev.trace_id:
                traces.add(ev.trace_id)
            if ev.session_id:
                sessions.add(ev.session_id)
        return {
            "total_events": total,
            "unique_traces": len(traces),
            "unique_sessions": len(sessions),
            "by_event_type": by_type,
            "by_severity": by_severity,
            "by_origin": by_origin,
        }
