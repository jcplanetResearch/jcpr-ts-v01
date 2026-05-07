"""
스모크 테스트 — _registry (Task 36)
====================================
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from src.agents.prompts._registry import (  # noqa: E402
    PromptRegistry,
    RegistryError,
    TemplateLoadError,
    TemplateNotFound,
)


# ─────────────────────────────────────────────────
# 헬퍼
# ─────────────────────────────────────────────────

def _make_md(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


VALID_MD = """---
template_id: market_analyst.test
version: v1.0
role: system
target_agent: market_analyst
description: "Test template"
required_variables: []
---

This is the body of the template.
"""


# ─────────────────────────────────────────────────
# 초기화
# ─────────────────────────────────────────────────

def test_registry_init_missing_dir():
    try:
        PromptRegistry(prompt_root=Path("/tmp/nonexistent_xyz_888"))
        assert False
    except RegistryError:
        pass
    print("✅ test_registry_init_missing_dir")


def test_registry_init_not_dir(tmp_dir):
    f = tmp_dir / "not_a_dir.txt"
    f.write_text("x")
    try:
        PromptRegistry(prompt_root=f)
        assert False
    except RegistryError:
        pass
    print("✅ test_registry_init_not_dir")


# ─────────────────────────────────────────────────
# 단일 조회
# ─────────────────────────────────────────────────

def test_registry_get_basic(tmp_dir):
    _make_md(tmp_dir / "system" / "test.md", VALID_MD)
    reg = PromptRegistry(prompt_root=tmp_dir)
    tmpl = reg.get("market_analyst.test")
    assert tmpl.template_id == "market_analyst.test"
    assert tmpl.version == "v1.0"
    print("✅ test_registry_get_basic")


def test_registry_get_not_found(tmp_dir):
    reg = PromptRegistry(prompt_root=tmp_dir)
    try:
        reg.get("nonexistent.template")
        assert False
    except TemplateNotFound:
        pass
    print("✅ test_registry_get_not_found")


def test_registry_get_invalid_id(tmp_dir):
    reg = PromptRegistry(prompt_root=tmp_dir)
    try:
        reg.get("no_dot")
        assert False
    except RegistryError:
        pass
    print("✅ test_registry_get_invalid_id")


def test_registry_caching(tmp_dir):
    _make_md(tmp_dir / "system" / "test.md", VALID_MD)
    reg = PromptRegistry(prompt_root=tmp_dir, cache_enabled=True)
    t1 = reg.get("market_analyst.test")
    t2 = reg.get("market_analyst.test")
    assert t1 is t2  # 같은 인스턴스
    reg.clear_cache()
    t3 = reg.get("market_analyst.test")
    assert t3 is not t1  # 캐시 클리어 후 새로 로드
    print("✅ test_registry_caching")


# ─────────────────────────────────────────────────
# 일괄 조회
# ─────────────────────────────────────────────────

def test_registry_list_all(tmp_dir):
    _make_md(tmp_dir / "system" / "a.md", VALID_MD.replace(
        "market_analyst.test", "market_analyst.a"
    ))
    _make_md(tmp_dir / "system" / "b.md", VALID_MD.replace(
        "market_analyst.test", "market_analyst.b"
    ))
    reg = PromptRegistry(prompt_root=tmp_dir)
    all_tmpls = reg.list_all()
    assert len(all_tmpls) == 2
    ids = sorted(t.template_id for t in all_tmpls)
    assert ids == ["market_analyst.a", "market_analyst.b"]
    print("✅ test_registry_list_all")


def test_registry_duplicate_id_rejected(tmp_dir):
    _make_md(tmp_dir / "a.md", VALID_MD)
    _make_md(tmp_dir / "b.md", VALID_MD)  # 같은 template_id
    reg = PromptRegistry(prompt_root=tmp_dir)
    try:
        reg.list_all()
        assert False
    except TemplateLoadError as e:
        assert "duplicate" in str(e).lower()
    print("✅ test_registry_duplicate_id_rejected")


def test_registry_list_by_agent(tmp_dir):
    _make_md(tmp_dir / "a.md", VALID_MD.replace(
        "market_analyst.test", "market_analyst.a"
    ))
    _make_md(tmp_dir / "b.md", VALID_MD.replace(
        "market_analyst.test", "risk_explainer.b"
    ).replace("market_analyst", "risk_explainer"))
    reg = PromptRegistry(prompt_root=tmp_dir)
    market = reg.list_by_agent("market_analyst")
    assert len(market) == 1
    assert market[0].template_id == "market_analyst.a"
    risk = reg.list_by_agent("risk_explainer")
    assert len(risk) == 1
    print("✅ test_registry_list_by_agent")


# ─────────────────────────────────────────────────
# 형식 오류
# ─────────────────────────────────────────────────

def test_registry_no_frontmatter(tmp_dir):
    _make_md(tmp_dir / "bad.md", "no frontmatter here")
    reg = PromptRegistry(prompt_root=tmp_dir)
    try:
        reg.list_all()
        assert False
    except TemplateLoadError as e:
        assert "frontmatter" in str(e).lower()
    print("✅ test_registry_no_frontmatter")


def test_registry_bad_yaml(tmp_dir):
    _make_md(tmp_dir / "bad.md", "---\ninvalid: yaml: too: many: colons\n---\nbody")
    reg = PromptRegistry(prompt_root=tmp_dir)
    try:
        reg.list_all()
        assert False
    except TemplateLoadError:
        pass
    print("✅ test_registry_bad_yaml")


def test_registry_missing_required_keys(tmp_dir):
    _make_md(tmp_dir / "bad.md", """---
template_id: agent.test
---
body""")
    reg = PromptRegistry(prompt_root=tmp_dir)
    try:
        reg.list_all()
        assert False
    except TemplateLoadError:
        pass
    print("✅ test_registry_missing_required_keys")


# ─────────────────────────────────────────────────
# response_schema 로드
# ─────────────────────────────────────────────────

def test_registry_with_schema(tmp_dir):
    schema_dir = tmp_dir / "schemas"
    schema_dir.mkdir()
    schema_path = schema_dir / "test.json"
    schema_path.write_text('{"type": "object"}')

    _make_md(tmp_dir / "system" / "test.md", """---
template_id: market_analyst.with_schema
version: v1.0
role: system
target_agent: market_analyst
response_schema_path: schemas/test.json
required_variables: []
---

body
""")
    reg = PromptRegistry(prompt_root=tmp_dir)
    tmpl = reg.get("market_analyst.with_schema")
    assert tmpl.response_schema is not None
    assert tmpl.response_schema["type"] == "object"
    print("✅ test_registry_with_schema")


def test_registry_schema_not_found(tmp_dir):
    _make_md(tmp_dir / "system" / "test.md", """---
template_id: market_analyst.bad_schema
version: v1.0
role: system
target_agent: market_analyst
response_schema_path: schemas/missing.json
required_variables: []
---

body
""")
    reg = PromptRegistry(prompt_root=tmp_dir)
    try:
        reg.list_all()
        assert False
    except TemplateLoadError as e:
        assert "schema" in str(e).lower() or "not found" in str(e).lower()
    print("✅ test_registry_schema_not_found")


# ─────────────────────────────────────────────────
# 실행
# ─────────────────────────────────────────────────

try:
    import pytest

    @pytest.fixture
    def tmp_dir(tmp_path):
        return tmp_path
except ImportError:
    pass


def _run_all() -> int:
    failed = 0
    no_arg = [test_registry_init_missing_dir]
    arg_tests = [
        test_registry_init_not_dir,
        test_registry_get_basic, test_registry_get_not_found,
        test_registry_get_invalid_id, test_registry_caching,
        test_registry_list_all, test_registry_duplicate_id_rejected,
        test_registry_list_by_agent,
        test_registry_no_frontmatter, test_registry_bad_yaml,
        test_registry_missing_required_keys,
        test_registry_with_schema, test_registry_schema_not_found,
    ]
    for fn in no_arg:
        try:
            fn()
        except AssertionError as e:
            print(f"❌ {fn.__name__}: {e}")
            failed += 1
        except Exception as e:  # noqa: BLE001
            print(f"💥 {fn.__name__}: {type(e).__name__}: {e}")
            failed += 1
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        for fn in arg_tests:
            sub = td_path / fn.__name__
            sub.mkdir()
            try:
                fn(sub)
            except AssertionError as e:
                print(f"❌ {fn.__name__}: {e}")
                failed += 1
            except Exception as e:  # noqa: BLE001
                print(f"💥 {fn.__name__}: {type(e).__name__}: {e}")
                failed += 1
    return failed


if __name__ == "__main__":
    print("─" * 50)
    print("Task 36 v0.1 — _registry 테스트")
    print("─" * 50)
    failed = _run_all()
    print("─" * 50)
    if failed == 0:
        print("✅ 모든 테스트 통과")
        sys.exit(0)
    else:
        print(f"❌ {failed}개 실패")
        sys.exit(1)
