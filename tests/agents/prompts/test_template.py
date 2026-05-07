"""
스모크 테스트 — _template (Task 36)
====================================
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from src.agents.prompts._template import (  # noqa: E402
    AGENT_COMMON,
    AGENT_MARKET_ANALYST,
    PromptTemplate,
    RenderedPrompt,
    ROLE_SYSTEM,
    ROLE_USER,
    extract_variables,
)


# ─────────────────────────────────────────────────
# extract_variables
# ─────────────────────────────────────────────────

def test_extract_variables_basic():
    assert extract_variables("Hello {{ name }}") == ["name"]
    assert extract_variables("{{ a }} and {{ b }}") == ["a", "b"]
    assert extract_variables("no variables here") == []
    print("✅ test_extract_variables_basic")


def test_extract_variables_dedup():
    assert extract_variables("{{ x }} {{ x }} {{ y }}") == ["x", "y"]
    print("✅ test_extract_variables_dedup")


def test_extract_variables_whitespace():
    assert extract_variables("{{name}}") == ["name"]
    assert extract_variables("{{  name  }}") == ["name"]
    assert extract_variables("{{\tname\t}}") == ["name"]
    print("✅ test_extract_variables_whitespace")


def test_extract_variables_empty():
    assert extract_variables("") == []
    print("✅ test_extract_variables_empty")


# ─────────────────────────────────────────────────
# PromptTemplate construction
# ─────────────────────────────────────────────────

def test_template_basic():
    tmpl = PromptTemplate(
        template_id="market_analyst.system",
        version="v1.0",
        role=ROLE_SYSTEM,
        body="Hello {{ name }}",
        required_variables=("name",),
        response_schema=None,
        target_agent=AGENT_MARKET_ANALYST,
    )
    assert tmpl.template_id == "market_analyst.system"
    assert tmpl.version == "v1.0"
    assert tmpl.role == ROLE_SYSTEM
    print("✅ test_template_basic")


def test_template_invalid_id():
    bad = ["", "no_dot", "with space", "trailing.", ".leading"]
    for b in bad:
        try:
            PromptTemplate(
                template_id=b,
                version="v1.0", role=ROLE_SYSTEM,
                body="x", required_variables=(),
                response_schema=None,
                target_agent=AGENT_COMMON,
            )
            assert False, f"Should reject {b!r}"
        except ValueError:
            pass
    print("✅ test_template_invalid_id")


def test_template_invalid_version():
    bad = ["1.0", "v", "version-1", "1"]
    for b in bad:
        try:
            PromptTemplate(
                template_id="agent.test",
                version=b, role=ROLE_SYSTEM,
                body="x", required_variables=(),
                response_schema=None,
                target_agent=AGENT_COMMON,
            )
            assert False, f"Should reject version {b!r}"
        except ValueError:
            pass
    print("✅ test_template_invalid_version")


def test_template_valid_versions():
    for v in ["v1", "v1.0", "v2.3.1"]:
        PromptTemplate(
            template_id="agent.test",
            version=v, role=ROLE_SYSTEM,
            body="x", required_variables=(),
            response_schema=None,
            target_agent=AGENT_COMMON,
        )
    print("✅ test_template_valid_versions")


def test_template_invalid_role():
    try:
        PromptTemplate(
            template_id="agent.test",
            version="v1.0", role="hacker",
            body="x", required_variables=(),
            response_schema=None,
            target_agent=AGENT_COMMON,
        )
        assert False
    except ValueError:
        pass
    print("✅ test_template_invalid_role")


def test_template_invalid_agent():
    try:
        PromptTemplate(
            template_id="agent.test",
            version="v1.0", role=ROLE_SYSTEM,
            body="x", required_variables=(),
            response_schema=None,
            target_agent="rogue_agent",
        )
        assert False
    except ValueError:
        pass
    print("✅ test_template_invalid_agent")


def test_template_undeclared_variable_in_body():
    """body의 변수가 required_variables에 없으면 거부."""
    try:
        PromptTemplate(
            template_id="agent.test",
            version="v1.0", role=ROLE_SYSTEM,
            body="Hello {{ name }} and {{ undeclared }}",
            required_variables=("name",),  # undeclared 누락
            response_schema=None,
            target_agent=AGENT_COMMON,
        )
        assert False, "Should reject undeclared body variable"
    except ValueError as e:
        assert "undeclared" in str(e).lower()
    print("✅ test_template_undeclared_variable_in_body")


def test_template_extra_declared_ok():
    """required_variables에는 있지만 body에 없는 것은 OK."""
    tmpl = PromptTemplate(
        template_id="agent.test",
        version="v1.0", role=ROLE_SYSTEM,
        body="Hello {{ name }}",
        required_variables=("name", "future_use"),
        response_schema=None,
        target_agent=AGENT_COMMON,
    )
    assert "future_use" in tmpl.required_variables
    print("✅ test_template_extra_declared_ok")


def test_template_invalid_variable_name():
    try:
        PromptTemplate(
            template_id="agent.test",
            version="v1.0", role=ROLE_SYSTEM,
            body="hi",
            required_variables=("123_starts_with_digit",),
            response_schema=None,
            target_agent=AGENT_COMMON,
        )
        assert False
    except ValueError:
        pass
    print("✅ test_template_invalid_variable_name")


def test_template_response_schema_must_be_dict():
    try:
        PromptTemplate(
            template_id="agent.test",
            version="v1.0", role=ROLE_SYSTEM,
            body="x", required_variables=(),
            response_schema="not a dict",  # type: ignore[arg-type]
            target_agent=AGENT_COMMON,
        )
        assert False
    except ValueError:
        pass
    print("✅ test_template_response_schema_must_be_dict")


def test_template_response_schema_needs_type():
    try:
        PromptTemplate(
            template_id="agent.test",
            version="v1.0", role=ROLE_SYSTEM,
            body="x", required_variables=(),
            response_schema={"properties": {}},  # 'type' 누락
            target_agent=AGENT_COMMON,
        )
        assert False
    except ValueError:
        pass
    print("✅ test_template_response_schema_needs_type")


def test_template_oversized_body():
    big = "x" * 200_000
    try:
        PromptTemplate(
            template_id="agent.test",
            version="v1.0", role=ROLE_SYSTEM,
            body=big, required_variables=(),
            response_schema=None,
            target_agent=AGENT_COMMON,
        )
        assert False
    except ValueError:
        pass
    print("✅ test_template_oversized_body")


def test_template_frozen():
    tmpl = PromptTemplate(
        template_id="agent.test",
        version="v1.0", role=ROLE_SYSTEM,
        body="hi", required_variables=(),
        response_schema=None,
        target_agent=AGENT_COMMON,
    )
    try:
        tmpl.version = "v2.0"  # type: ignore[misc]
        assert False
    except Exception:
        pass
    print("✅ test_template_frozen")


def test_template_to_dict_summary():
    tmpl = PromptTemplate(
        template_id="agent.test",
        version="v1.0", role=ROLE_SYSTEM,
        body="hi {{ name }}",
        required_variables=("name",),
        response_schema={"type": "object"},
        target_agent=AGENT_COMMON,
        description="test template",
    )
    d = tmpl.to_dict()
    assert d["template_id"] == "agent.test"
    assert "body" in d
    s = tmpl.summary()
    assert "body" not in s
    assert s["has_response_schema"] is True
    assert s["body_length"] > 0
    print("✅ test_template_to_dict_summary")


# ─────────────────────────────────────────────────
# RenderedPrompt
# ─────────────────────────────────────────────────

def test_rendered_prompt_basic():
    rp = RenderedPrompt(
        template_id="agent.test",
        version="v1.0",
        rendered_text="hello world",
        variables_used={"name": "world"},
        rendered_at_utc=datetime.now(timezone.utc),
    )
    assert rp.template_id == "agent.test"
    assert rp.rendered_text == "hello world"
    print("✅ test_rendered_prompt_basic")


def test_rendered_prompt_naive_datetime_rejected():
    try:
        RenderedPrompt(
            template_id="agent.test",
            version="v1.0",
            rendered_text="x",
            variables_used={},
            rendered_at_utc=datetime(2026, 5, 7),  # naive
        )
        assert False
    except ValueError:
        pass
    print("✅ test_rendered_prompt_naive_datetime_rejected")


# ─────────────────────────────────────────────────
# 실행
# ─────────────────────────────────────────────────

def _run_all() -> int:
    failed = 0
    tests = [
        test_extract_variables_basic,
        test_extract_variables_dedup,
        test_extract_variables_whitespace,
        test_extract_variables_empty,
        test_template_basic,
        test_template_invalid_id,
        test_template_invalid_version,
        test_template_valid_versions,
        test_template_invalid_role,
        test_template_invalid_agent,
        test_template_undeclared_variable_in_body,
        test_template_extra_declared_ok,
        test_template_invalid_variable_name,
        test_template_response_schema_must_be_dict,
        test_template_response_schema_needs_type,
        test_template_oversized_body,
        test_template_frozen,
        test_template_to_dict_summary,
        test_rendered_prompt_basic,
        test_rendered_prompt_naive_datetime_rejected,
    ]
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            print(f"❌ {fn.__name__}: {e}")
            failed += 1
        except Exception as e:  # noqa: BLE001
            print(f"💥 {fn.__name__}: {type(e).__name__}: {e}")
            failed += 1
    return failed


if __name__ == "__main__":
    print("─" * 50)
    print("Task 36 v0.1 — _template 테스트")
    print("─" * 50)
    failed = _run_all()
    print("─" * 50)
    if failed == 0:
        print("✅ 모든 테스트 통과")
        sys.exit(0)
    else:
        print(f"❌ {failed}개 실패")
        sys.exit(1)
