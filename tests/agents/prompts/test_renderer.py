"""
스모크 테스트 — _renderer (Task 36)
====================================
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from src.agents.prompts._template import (  # noqa: E402
    AGENT_COMMON,
    PromptTemplate,
    ROLE_USER,
)
from src.agents.prompts._renderer import (  # noqa: E402
    MAX_SINGLE_VALUE_BYTES,
    MAX_TOTAL_RENDERED_BYTES,
    RenderError,
    SecretInVariableError,
    ValueTooLargeError,
    VariableMissingError,
    safe_render,
)


def _tmpl(body: str, variables: tuple = ()) -> PromptTemplate:
    return PromptTemplate(
        template_id="agent.test",
        version="v1.0",
        role=ROLE_USER,
        body=body,
        required_variables=variables,
        response_schema=None,
        target_agent=AGENT_COMMON,
    )


# ─────────────────────────────────────────────────
# 기본
# ─────────────────────────────────────────────────

def test_render_basic():
    tmpl = _tmpl("Hello {{ name }}", ("name",))
    rp = safe_render(tmpl, {"name": "Alice"})
    assert rp.rendered_text == "Hello Alice"
    print("✅ test_render_basic")


def test_render_multiple_vars():
    tmpl = _tmpl("{{ greeting }}, {{ name }}!", ("greeting", "name"))
    rp = safe_render(tmpl, {"greeting": "Hi", "name": "Bob"})
    assert rp.rendered_text == "Hi, Bob!"
    print("✅ test_render_multiple_vars")


def test_render_repeated_vars():
    tmpl = _tmpl("{{ x }} - {{ x }} - {{ x }}", ("x",))
    rp = safe_render(tmpl, {"x": "ABC"})
    assert rp.rendered_text == "ABC - ABC - ABC"
    print("✅ test_render_repeated_vars")


def test_render_no_vars():
    tmpl = _tmpl("static text")
    rp = safe_render(tmpl, {})
    assert rp.rendered_text == "static text"
    print("✅ test_render_no_vars")


# ─────────────────────────────────────────────────
# 타입 처리
# ─────────────────────────────────────────────────

def test_render_int_value():
    tmpl = _tmpl("count = {{ n }}", ("n",))
    rp = safe_render(tmpl, {"n": 42})
    assert rp.rendered_text == "count = 42"
    print("✅ test_render_int_value")


def test_render_float_value():
    tmpl = _tmpl("price = {{ p }}", ("p",))
    rp = safe_render(tmpl, {"p": 3.14})
    assert "3.14" in rp.rendered_text
    print("✅ test_render_float_value")


def test_render_decimal_value():
    tmpl = _tmpl("amount = {{ a }}", ("a",))
    rp = safe_render(tmpl, {"a": Decimal("70000.50")})
    assert rp.rendered_text == "amount = 70000.50"
    print("✅ test_render_decimal_value")


def test_render_bool_value():
    tmpl = _tmpl("active = {{ flag }}", ("flag",))
    rp = safe_render(tmpl, {"flag": True})
    assert rp.rendered_text == "active = true"
    rp2 = safe_render(tmpl, {"flag": False})
    assert rp2.rendered_text == "active = false"
    print("✅ test_render_bool_value")


def test_render_none_value():
    tmpl = _tmpl("v = {{ x }}", ("x",))
    rp = safe_render(tmpl, {"x": None})
    assert "(none)" in rp.rendered_text
    print("✅ test_render_none_value")


def test_render_dict_rejected():
    """dict는 명시적 거부 (LLM에 구조화 직접 전달 금지)."""
    tmpl = _tmpl("{{ x }}", ("x",))
    try:
        safe_render(tmpl, {"x": {"key": "val"}})
        assert False
    except RenderError:
        pass
    print("✅ test_render_dict_rejected")


def test_render_list_rejected():
    tmpl = _tmpl("{{ x }}", ("x",))
    try:
        safe_render(tmpl, {"x": [1, 2, 3]})
        assert False
    except RenderError:
        pass
    print("✅ test_render_list_rejected")


# ─────────────────────────────────────────────────
# 보안: 시크릿 차단
# ─────────────────────────────────────────────────

def test_render_secret_key_blocked():
    """변수 키에 시크릿 키워드 → 거부."""
    tmpl = _tmpl("{{ name }}", ("name",))
    bad_keys = ["api_key", "password", "token", "secret_data", "auth"]
    for k in bad_keys:
        try:
            safe_render(tmpl, {"name": "ok", k: "x"})
            assert False, f"Should reject key {k!r}"
        except SecretInVariableError:
            pass
    print("✅ test_render_secret_key_blocked")


def test_render_credential_value_masked():
    """긴 base64-like 값 자동 마스킹."""
    tmpl = _tmpl("token = {{ name }}", ("name",))
    long_b64 = "ABCDEFGH1234" * 5  # 60자
    rp = safe_render(tmpl, {"name": long_b64})
    # 마스킹됨
    assert long_b64 not in rp.rendered_text
    assert "MASKED" in rp.rendered_text
    print("✅ test_render_credential_value_masked")


def test_render_pii_key_masked_in_audit():
    """PII 키는 변수_used에서 마스킹."""
    tmpl = _tmpl("user = {{ name }}", ("name",))
    rp = safe_render(tmpl, {"name": "alice", "operator_id_full": "alice@x.com"},
                     allow_extra_variables=True)
    # 본문에는 'name'만 (operator_id_full 자리표시자 없음)
    assert "alice" in rp.rendered_text
    # variables_used에서 PII 마스킹
    assert "MASKED" in rp.variables_used["operator_id_full"]
    print("✅ test_render_pii_key_masked_in_audit")


# ─────────────────────────────────────────────────
# 누락 / 추가
# ─────────────────────────────────────────────────

def test_render_missing_variable():
    tmpl = _tmpl("{{ a }} {{ b }}", ("a", "b"))
    try:
        safe_render(tmpl, {"a": "1"})  # b 누락
        assert False
    except VariableMissingError:
        pass
    print("✅ test_render_missing_variable")


def test_render_extra_variable_rejected():
    tmpl = _tmpl("{{ name }}", ("name",))
    try:
        safe_render(tmpl, {"name": "x", "extra": "y"})
        assert False
    except RenderError:
        pass
    print("✅ test_render_extra_variable_rejected")


def test_render_extra_variable_allowed():
    tmpl = _tmpl("{{ name }}", ("name",))
    rp = safe_render(
        tmpl, {"name": "x", "extra": "y"},
        allow_extra_variables=True,
    )
    assert "x" in rp.rendered_text
    print("✅ test_render_extra_variable_allowed")


# ─────────────────────────────────────────────────
# 크기 제한
# ─────────────────────────────────────────────────

def test_render_oversized_value():
    tmpl = _tmpl("{{ data }}", ("data",))
    big = "a" * (MAX_SINGLE_VALUE_BYTES + 100)
    try:
        safe_render(tmpl, {"data": big})
        assert False
    except ValueTooLargeError:
        pass
    print("✅ test_render_oversized_value")


def test_render_too_many_variables():
    """변수 100개 초과 → 거부."""
    body = " ".join(f"{{{{ v{i} }}}}" for i in range(150))
    tmpl = PromptTemplate(
        template_id="agent.test",
        version="v1.0",
        role=ROLE_USER,
        body=body,
        required_variables=tuple(f"v{i}" for i in range(150)),
        response_schema=None,
        target_agent=AGENT_COMMON,
    )
    vars_dict = {f"v{i}": str(i) for i in range(150)}
    try:
        safe_render(tmpl, vars_dict)
        assert False
    except RenderError as e:
        assert "too many" in str(e).lower()
    print("✅ test_render_too_many_variables")


# ─────────────────────────────────────────────────
# 결과 메타
# ─────────────────────────────────────────────────

def test_render_result_metadata():
    tmpl = _tmpl("hi {{ name }}", ("name",))
    rp = safe_render(tmpl, {"name": "world"})
    assert rp.template_id == "agent.test"
    assert rp.version == "v1.0"
    assert rp.variables_used == {"name": "world"}
    assert rp.rendered_at_utc.tzinfo is not None
    print("✅ test_render_result_metadata")


def test_render_invalid_template_arg():
    try:
        safe_render({"not": "a template"}, {})  # type: ignore[arg-type]
        assert False
    except RenderError:
        pass
    print("✅ test_render_invalid_template_arg")


def test_render_non_dict_variables():
    tmpl = _tmpl("hi", ())
    try:
        safe_render(tmpl, "not a dict")  # type: ignore[arg-type]
        assert False
    except RenderError:
        pass
    print("✅ test_render_non_dict_variables")


def test_render_non_str_key():
    tmpl = _tmpl("hi", ())
    try:
        safe_render(tmpl, {123: "v"}, allow_extra_variables=True)  # type: ignore[dict-item]
        assert False
    except RenderError:
        pass
    print("✅ test_render_non_str_key")


# ─────────────────────────────────────────────────
# 실행
# ─────────────────────────────────────────────────

def _run_all() -> int:
    failed = 0
    tests = [
        test_render_basic, test_render_multiple_vars, test_render_repeated_vars,
        test_render_no_vars,
        test_render_int_value, test_render_float_value,
        test_render_decimal_value, test_render_bool_value,
        test_render_none_value,
        test_render_dict_rejected, test_render_list_rejected,
        test_render_secret_key_blocked,
        test_render_credential_value_masked,
        test_render_pii_key_masked_in_audit,
        test_render_missing_variable,
        test_render_extra_variable_rejected,
        test_render_extra_variable_allowed,
        test_render_oversized_value,
        test_render_too_many_variables,
        test_render_result_metadata,
        test_render_invalid_template_arg,
        test_render_non_dict_variables,
        test_render_non_str_key,
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
    print("Task 36 v0.1 — _renderer 테스트")
    print("─" * 50)
    failed = _run_all()
    print("─" * 50)
    if failed == 0:
        print("✅ 모든 테스트 통과")
        sys.exit(0)
    else:
        print(f"❌ {failed}개 실패")
        sys.exit(1)
