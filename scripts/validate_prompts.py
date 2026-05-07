#!/usr/bin/env python3
"""
프롬프트 검증 CLI (Prompt Validation CLI)
==========================================

JCPR Trading System - jcpr-ts-v01
Task 36 v0.1

모든 프롬프트 템플릿 형식 + schema 검증.
(Validates all prompt templates' format + schemas.)

사용 (Usage):
    python scripts/validate_prompts.py
    python scripts/validate_prompts.py --json
    python scripts/validate_prompts.py --prompt-root /custom/path
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
        description="JCPR Prompt Template Validator (Task 36 v0.1)",
    )
    p.add_argument("--prompt-root", default=None,
                   help="프롬프트 루트 (default: src/agents/prompts/)")
    p.add_argument("--json", action="store_true", help="JSON 출력")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    args = _parse_args(argv)

    from src.agents.prompts import (
        PromptRegistry,
        DEFAULT_PROMPT_ROOT,
        TemplateLoadError,
    )

    root = Path(args.prompt_root) if args.prompt_root else DEFAULT_PROMPT_ROOT

    try:
        registry = PromptRegistry(prompt_root=root)
    except Exception as e:  # noqa: BLE001
        print(f"❌ Failed to init registry: {e}", file=sys.stderr)
        return 1

    errors: list[str] = []
    summaries: list[dict] = []

    try:
        all_templates = registry.list_all()
    except TemplateLoadError as e:
        errors.append(str(e))
        all_templates = []

    for tmpl in all_templates:
        summaries.append(tmpl.summary())

    # 통계
    by_role: dict[str, int] = {}
    by_agent: dict[str, int] = {}
    with_schema = 0
    for t in all_templates:
        by_role[t.role] = by_role.get(t.role, 0) + 1
        by_agent[t.target_agent] = by_agent.get(t.target_agent, 0) + 1
        if t.response_schema:
            with_schema += 1

    output = {
        "ok": len(errors) == 0,
        "prompt_root": str(root),
        "total_templates": len(all_templates),
        "with_schema": with_schema,
        "by_role": by_role,
        "by_agent": by_agent,
        "templates": summaries,
        "errors": errors,
    }

    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"━━━ Prompt Templates ━━━")
        print(f"Root: {root}")
        print(f"Total: {len(all_templates)}")
        print(f"With response_schema: {with_schema}")
        print(f"\nBy role:")
        for k, v in sorted(by_role.items()):
            print(f"  {k:15s} {v}")
        print(f"\nBy target agent:")
        for k, v in sorted(by_agent.items()):
            print(f"  {k:20s} {v}")
        print(f"\n━━━ Templates ━━━")
        for s in summaries:
            schema_marker = "📋" if s["has_response_schema"] else "  "
            print(f"  {schema_marker} {s['template_id']:50s} "
                  f"{s['version']:6s} "
                  f"{s['role']:12s} "
                  f"vars={len(s['required_variables'])}")
        if errors:
            print(f"\n━━━ ERRORS ({len(errors)}) ━━━")
            for e in errors:
                print(f"  ❌ {e}")

    return 0 if output["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
