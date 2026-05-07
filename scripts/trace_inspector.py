#!/usr/bin/env python3
"""
추적 검사 CLI (Trace Inspector CLI)
=====================================

JCPR Trading System - jcpr-ts-v01
Task A3 v0.1

감사 로그에서 trace_id 또는 조건으로 이벤트 조회.
(Inspect audit log events by trace_id or filters.)

사용 (Usage):
    # 단일 trace 전체 경로
    python scripts/trace_inspector.py --audit-dir data/audit \\
        --trace-id trc-20260507-a1b2c3d4

    # 트리 형식 출력
    python scripts/trace_inspector.py --audit-dir data/audit \\
        --trace-id trc-20260507-a1b2c3d4 --tree

    # 세션 내 모든 trace 요약
    python scripts/trace_inspector.py --audit-dir data/audit \\
        --session-id session-2026-05-07 --list

    # 예외 발생 trace만
    python scripts/trace_inspector.py --audit-dir data/audit \\
        --session-id session-2026-05-07 --list --only-exceptions

    # 일자 통계
    python scripts/trace_inspector.py --audit-dir data/audit --stats

    # JSON 출력 (자동화용)
    python scripts/trace_inspector.py --audit-dir data/audit \\
        --trace-id trc-20260507-a1b2c3d4 --json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# repo path
_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from src.observability import (  # noqa: E402
    AuditIndexer,
    SpanNode,
    TraceSummary,
)


# ─────────────────────────────────────────────────
# 출력 헬퍼 (Output Helpers)
# ─────────────────────────────────────────────────

def _format_event(ev) -> str:
    """단일 이벤트 한 줄 요약."""
    ts = ev.timestamp_utc.strftime("%H:%M:%S.%f")[:-3]
    sev_emoji = {
        "debug": "  ",
        "info": "ℹ️ ",
        "warning": "⚠️ ",
        "error": "❌ ",
        "critical": "🚨 ",
    }.get(ev.severity, "  ")
    span_short = ev.span_id[-8:]
    parent_short = ev.parent_span_id[-8:] if ev.parent_span_id else "ROOT"
    return (
        f"{sev_emoji}[{ts}] "
        f"{ev.event_type:25s} "
        f"span={span_short} "
        f"parent={parent_short} "
        f"origin={ev.origin}"
    )


def _print_tree(node: SpanNode, indent: int = 0) -> None:
    """트리 재귀 출력."""
    prefix = "  " * indent + ("└─ " if indent > 0 else "")
    line = _format_event(node.event)
    print(f"{prefix}{line}")
    # payload 핵심 키만
    payload = node.event.payload
    if payload:
        keys_to_show = list(payload.keys())[:5]
        if keys_to_show:
            kv = ", ".join(f"{k}={payload.get(k)!r:.40s}" for k in keys_to_show)
            print(f"{'  ' * (indent + 1)}└─ payload: {kv}")
    for child in node.children:
        _print_tree(child, indent + 1)


def _print_summary(s: TraceSummary) -> None:
    """trace 요약 출력."""
    flags = []
    if s.has_critical:
        flags.append("🚨 CRITICAL")
    if s.has_exceptions:
        flags.append("❌ EXCEPTION")
    flag_str = " ".join(flags) if flags else "✅"
    duration_str = (
        f"{s.duration_ms / 1000:.2f}s" if s.duration_ms >= 1000
        else f"{s.duration_ms:.1f}ms"
    )
    print(f"  {s.trace_id} [{flag_str}]")
    print(f"    Origin: {s.origin}, Session: {s.session_id}")
    print(f"    Events: {s.event_count}, Spans: {s.span_count}, "
          f"Duration: {duration_str}")
    print(f"    Start: {s.start_utc.isoformat()}")
    if s.event_types:
        et_str = ", ".join(f"{k}={v}" for k, v in
                           sorted(s.event_types.items(), key=lambda x: -x[1])[:5])
        print(f"    Types: {et_str}")


# ─────────────────────────────────────────────────
# 인자 파싱 (Argparse)
# ─────────────────────────────────────────────────

def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="JCPR Trace Inspector (Task A3 v0.1)",
    )
    p.add_argument("--audit-dir", required=True,
                   help="감사 로그 디렉터리")

    g = p.add_mutually_exclusive_group()
    g.add_argument("--trace-id", help="단일 trace_id 조회")
    g.add_argument("--list", action="store_true", help="trace 목록")
    g.add_argument("--stats", action="store_true", help="통계")

    p.add_argument("--session-id", help="세션 필터")
    p.add_argument("--since", help="시작 시각 ISO (UTC)")
    p.add_argument("--until", help="종료 시각 ISO (UTC)")
    p.add_argument("--origin", help="origin 필터")
    p.add_argument("--symbol", help="종목 필터")
    p.add_argument("--strategy-id", help="전략 필터")
    p.add_argument("--event-type", action="append",
                   help="이벤트 타입 (반복 가능)")
    p.add_argument("--severity", action="append",
                   help="severity (반복 가능)")
    p.add_argument("--only-exceptions", action="store_true",
                   help="예외 발생 trace만")
    p.add_argument("--only-critical", action="store_true",
                   help="critical 발생 trace만")

    p.add_argument("--tree", action="store_true",
                   help="트리 형식 출력 (--trace-id 시)")
    p.add_argument("--json", action="store_true",
                   help="JSON 출력")
    p.add_argument("--limit", type=int, default=100, help="최대 결과 수")
    return p.parse_args(argv)


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


# ─────────────────────────────────────────────────
# 메인 (Main)
# ─────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    args = _parse_args(argv)

    indexer = AuditIndexer(audit_dir=args.audit_dir)

    since = _parse_iso(args.since)
    until = _parse_iso(args.until)

    # ─── --trace-id ────────────────────────────
    if args.trace_id:
        if args.tree:
            tree = indexer.build_trace_tree(args.trace_id)
            if tree is None:
                print(f"❌ trace_id {args.trace_id} not found", file=sys.stderr)
                return 1
            if args.json:
                print(json.dumps(tree.to_dict(), ensure_ascii=False, indent=2,
                                 default=str))
            else:
                summary = indexer.trace_summary(args.trace_id)
                if summary:
                    print("━━━ Trace Summary ━━━")
                    _print_summary(summary)
                    print()
                print("━━━ Span Tree ━━━")
                _print_tree(tree)
            return 0
        else:
            events = indexer.find_by_trace(args.trace_id)
            if not events:
                print(f"❌ trace_id {args.trace_id} not found", file=sys.stderr)
                return 1
            if args.json:
                print(json.dumps([e.to_dict() for e in events],
                                 ensure_ascii=False, indent=2, default=str))
            else:
                summary = indexer.trace_summary(args.trace_id)
                if summary:
                    print("━━━ Trace Summary ━━━")
                    _print_summary(summary)
                    print()
                print(f"━━━ Events ({len(events)}) ━━━")
                for ev in events:
                    print(_format_event(ev))
            return 0

    # ─── --list ────────────────────────────────
    if args.list:
        summaries = indexer.list_traces(
            session_id=args.session_id,
            since_utc=since,
            until_utc=until,
            only_with_exceptions=args.only_exceptions,
            only_with_critical=args.only_critical,
            limit=args.limit,
        )
        if not summaries:
            print("No traces found.")
            return 0
        if args.json:
            print(json.dumps([s.to_dict() for s in summaries],
                             ensure_ascii=False, indent=2, default=str))
        else:
            print(f"━━━ Traces ({len(summaries)}) ━━━")
            for s in summaries:
                _print_summary(s)
                print()
        return 0

    # ─── --stats ───────────────────────────────
    if args.stats:
        stats = indexer.stats(since_utc=since, until_utc=until)
        if args.json:
            print(json.dumps(stats, ensure_ascii=False, indent=2, default=str))
        else:
            print("━━━ Statistics ━━━")
            print(f"Total events:      {stats['total_events']:,}")
            print(f"Unique traces:     {stats['unique_traces']:,}")
            print(f"Unique sessions:   {stats['unique_sessions']:,}")
            print(f"\nBy event type:")
            for k, v in sorted(stats['by_event_type'].items(),
                               key=lambda x: -x[1])[:15]:
                print(f"  {k:30s} {v:,}")
            print(f"\nBy severity:")
            for k, v in stats['by_severity'].items():
                print(f"  {k:15s} {v:,}")
            print(f"\nBy origin:")
            for k, v in stats['by_origin'].items():
                print(f"  {k:20s} {v:,}")
        return 0

    # ─── 기본: 일반 검색 ───────────────────────
    events = indexer.search(
        session_id=args.session_id,
        origin=args.origin,
        event_types=set(args.event_type) if args.event_type else None,
        severities=set(args.severity) if args.severity else None,
        since_utc=since,
        until_utc=until,
        symbol=args.symbol,
        strategy_id=args.strategy_id,
        limit=args.limit,
    )
    if args.json:
        print(json.dumps([e.to_dict() for e in events],
                         ensure_ascii=False, indent=2, default=str))
    else:
        print(f"━━━ Events ({len(events)}) ━━━")
        for ev in events:
            print(_format_event(ev))

    return 0


if __name__ == "__main__":
    sys.exit(main())
