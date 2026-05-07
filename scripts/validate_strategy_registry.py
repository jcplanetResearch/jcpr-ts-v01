#!/usr/bin/env python3
"""
전략 레지스트리 검증 CLI (Strategy Registry Validator CLI)
============================================================

JCPR Trading System - jcpr-ts-v01
Task 45 v0.1

YAML 파일을 검증하고 요약 출력.
(Validates YAML and prints summary.)

사용 (Usage):
    python scripts/validate_strategy_registry.py configs/strategy_registry.yaml
    python scripts/validate_strategy_registry.py --json configs/strategy_registry.example.yaml
    python scripts/validate_strategy_registry.py --check-imports configs/strategy_registry.yaml
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# repo path
_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from src.strategies import RegistryLoadError, load_registry  # noqa: E402


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="JCPR Strategy Registry Validator (Task 45 v0.1)",
    )
    p.add_argument("path", help="strategy_registry.yaml 경로")
    p.add_argument("--json", action="store_true",
                   help="JSON 형식으로 요약 출력")
    p.add_argument("--check-imports", action="store_true",
                   help="활성 전략의 module_path import 시도 (실제 클래스 로드)")
    p.add_argument("--quiet", "-q", action="store_true",
                   help="요약 생략 — 검증만")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    args = _parse_args(argv)

    # ─── 로드 + 검증 ──────────────────────────
    try:
        registry = load_registry(args.path)
    except RegistryLoadError as e:
        print(f"❌ 검증 실패 (Validation failed):\n{e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"💥 예상치 못한 오류 (Unexpected error): "
              f"{type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print(f"✅ 검증 통과 (Validation passed): {args.path}")
    print(f"   전체 전략 (Total): {len(registry)}")
    print(f"   활성 (Active): {len(registry.list_active())}")
    print(f"   라이브 적격 (Live-eligible): {len(registry.list_live_eligible())}")
    print(f"   페이퍼 전용 (Paper-only): {len(registry.list_paper_only())}")
    print(f"   활성 자본 가중치 합 (Sum): "
          f"{registry.total_capital_weight()}")

    # ─── Import 검증 (옵션) ───────────────────
    if args.check_imports:
        print()
        print("━━━ Import 검증 (--check-imports) ━━━")
        import_errors = 0
        for entry in registry.list_active():
            try:
                cls = entry.load_class()
                print(f"   ✅ {entry.strategy_id}: "
                      f"{entry.module_path}.{entry.class_name} "
                      f"-> {cls.__name__}")
            except (ImportError, AttributeError, TypeError) as e:
                print(f"   ❌ {entry.strategy_id}: {type(e).__name__}: {e}",
                      file=sys.stderr)
                import_errors += 1
        if import_errors > 0:
            print(f"\n❌ {import_errors}개 전략 import 실패", file=sys.stderr)
            return 2

    # ─── 요약 출력 ────────────────────────────
    if not args.quiet:
        print()
        if args.json:
            summary = registry.summary()
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            print("━━━ 전략 목록 ━━━")
            for e in registry.list_all():
                status = []
                if e.enabled:
                    status.append("✅ enabled")
                else:
                    status.append("⚪ disabled")
                if e.paper_only:
                    status.append("📝 paper_only")
                else:
                    status.append("💼 live")
                status_str = " | ".join(status)
                print(f"   {e.strategy_id} v{e.version} [{e.timeframe}] "
                      f"weight={e.capital_weight}")
                print(f"      {status_str}")
                print(f"      universe: {len(e.universe)} symbols, "
                      f"categories: {e.signal_categories}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
