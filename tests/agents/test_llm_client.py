"""
스모크 테스트 — _llm_client (Task 37)
======================================
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from src.agents._llm_client import (  # noqa: E402
    LLMClient,
    LLMRequest,
    LLMResponse,
    MockLLMClient,
    parse_json_response,
    validate_response,
)


# ─────────────────────────────────────────────────
# LLMRequest
# ─────────────────────────────────────────────────

def test_request_basic():
    req = LLMRequest(
        system_prompt="You are X",
        user_prompt="Do Y",
    )
    assert req.system_prompt == "You are X"
    assert req.temperature == 0.0
    assert req.max_tokens == 4096
    print("✅ test_request_basic")


def test_request_empty_rejected():
    try:
        LLMRequest(system_prompt="", user_prompt="x")
        assert False
    except ValueError:
        pass
    try:
        LLMRequest(system_prompt="x", user_prompt="")
        assert False
    except ValueError:
        pass
    print("✅ test_request_empty_rejected")


def test_request_invalid_max_tokens():
    try:
        LLMRequest(system_prompt="x", user_prompt="y", max_tokens=0)
        assert False
    except ValueError:
        pass
    try:
        LLMRequest(system_prompt="x", user_prompt="y", max_tokens=99999)
        assert False
    except ValueError:
        pass
    print("✅ test_request_invalid_max_tokens")


def test_request_invalid_temperature():
    try:
        LLMRequest(system_prompt="x", user_prompt="y", temperature=-0.5)
        assert False
    except ValueError:
        pass
    try:
        LLMRequest(system_prompt="x", user_prompt="y", temperature=3.0)
        assert False
    except ValueError:
        pass
    print("✅ test_request_invalid_temperature")


# ─────────────────────────────────────────────────
# LLMResponse
# ─────────────────────────────────────────────────

def test_response_basic():
    resp = LLMResponse(
        raw_text='{"x": 1}',
        parsed_json={"x": 1},
        parse_error=None,
        schema_validated=True,
        schema_error=None,
        model_id="test",
        elapsed_ms=10.0,
        received_at_utc=datetime.now(timezone.utc),
    )
    assert resp.is_success
    print("✅ test_response_basic")


def test_response_naive_datetime_rejected():
    try:
        LLMResponse(
            raw_text="x",
            parsed_json=None,
            parse_error=None,
            schema_validated=False,
            schema_error=None,
            model_id="test",
            elapsed_ms=0.0,
            received_at_utc=datetime(2026, 5, 7),  # naive
        )
        assert False
    except ValueError:
        pass
    print("✅ test_response_naive_datetime_rejected")


def test_response_is_success_false():
    resp = LLMResponse(
        raw_text="not json",
        parsed_json=None,
        parse_error="parse fail",
        schema_validated=False,
        schema_error=None,
        model_id="test",
        elapsed_ms=0.0,
        received_at_utc=datetime.now(timezone.utc),
    )
    assert not resp.is_success
    print("✅ test_response_is_success_false")


def test_response_to_dict_no_raw_text():
    resp = LLMResponse(
        raw_text="x" * 100,
        parsed_json={},
        parse_error=None,
        schema_validated=True,
        schema_error=None,
        model_id="test",
        elapsed_ms=1.0,
        received_at_utc=datetime.now(timezone.utc),
    )
    d = resp.to_dict()
    assert "raw_text" not in d  # raw_text는 to_dict에 포함 안 됨
    assert d["raw_text_length"] == 100
    print("✅ test_response_to_dict_no_raw_text")


# ─────────────────────────────────────────────────
# parse_json_response
# ─────────────────────────────────────────────────

def test_parse_json_direct():
    parsed, err = parse_json_response('{"a": 1}')
    assert parsed == {"a": 1}
    assert err is None
    print("✅ test_parse_json_direct")


def test_parse_json_markdown_block():
    text = "Here is the answer:\n```json\n{\"x\": 2}\n```\nThanks"
    parsed, err = parse_json_response(text)
    assert parsed == {"x": 2}
    print("✅ test_parse_json_markdown_block")


def test_parse_json_markdown_no_lang():
    text = "Result:\n```\n{\"y\": 3}\n```"
    parsed, err = parse_json_response(text)
    assert parsed == {"y": 3}
    print("✅ test_parse_json_markdown_no_lang")


def test_parse_json_braces_extraction():
    text = 'preamble {"z": 4} epilogue'
    parsed, err = parse_json_response(text)
    assert parsed == {"z": 4}
    print("✅ test_parse_json_braces_extraction")


def test_parse_json_nested():
    text = '{"outer": {"inner": "value"}}'
    parsed, err = parse_json_response(text)
    assert parsed["outer"]["inner"] == "value"
    print("✅ test_parse_json_nested")


def test_parse_json_string_with_braces():
    """문자열 안의 } 가 깊이 계산을 깨면 안 됨."""
    text = '{"text": "with {} braces", "x": 1}'
    parsed, err = parse_json_response(text)
    assert parsed["text"] == "with {} braces"
    assert parsed["x"] == 1
    print("✅ test_parse_json_string_with_braces")


def test_parse_json_empty():
    parsed, err = parse_json_response("")
    assert parsed is None
    assert err is not None
    print("✅ test_parse_json_empty")


def test_parse_json_unbalanced():
    parsed, err = parse_json_response('{"x": 1')
    assert parsed is None
    print("✅ test_parse_json_unbalanced")


def test_parse_json_top_level_array_rejected():
    parsed, err = parse_json_response("[1, 2, 3]")
    assert parsed is None
    print("✅ test_parse_json_top_level_array_rejected")


# ─────────────────────────────────────────────────
# validate_response
# ─────────────────────────────────────────────────

def test_validate_basic_pass():
    schema = {
        "type": "object",
        "required": ["x"],
        "properties": {"x": {"type": "integer"}},
    }
    ok, err = validate_response({"x": 1}, schema)
    assert ok
    assert err is None
    print("✅ test_validate_basic_pass")


def test_validate_missing_required():
    schema = {
        "type": "object",
        "required": ["x"],
        "properties": {"x": {"type": "integer"}},
    }
    ok, err = validate_response({}, schema)
    assert not ok
    assert "x" in err
    print("✅ test_validate_missing_required")


def test_validate_wrong_type():
    schema = {
        "type": "object",
        "properties": {"x": {"type": "integer"}},
    }
    ok, err = validate_response({"x": "string"}, schema)
    assert not ok
    print("✅ test_validate_wrong_type")


# ─────────────────────────────────────────────────
# MockLLMClient
# ─────────────────────────────────────────────────

def test_mock_canned():
    client = MockLLMClient(canned_response='{"hello": "world"}')
    req = LLMRequest(system_prompt="x", user_prompt="y")
    resp = client.invoke(req)
    assert resp.parsed_json == {"hello": "world"}
    assert len(client.call_history) == 1
    print("✅ test_mock_canned")


def test_mock_schema_based():
    client = MockLLMClient(schema_based=True)
    schema = {
        "type": "object",
        "required": ["summary_ko"],
        "properties": {
            "summary_ko": {"type": "string"},
            "count": {"type": "integer"},
        },
    }
    req = LLMRequest(
        system_prompt="x", user_prompt="y",
        response_schema=schema,
    )
    resp = client.invoke(req)
    assert resp.parsed_json is not None
    assert "summary_ko" in resp.parsed_json
    assert resp.is_success
    print("✅ test_mock_schema_based")


def test_mock_parse_fail_mode():
    client = MockLLMClient(fail_mode="parse_fail")
    req = LLMRequest(system_prompt="x", user_prompt="y")
    resp = client.invoke(req)
    assert resp.parsed_json is None
    assert not resp.is_success
    print("✅ test_mock_parse_fail_mode")


def test_mock_schema_fail_mode():
    client = MockLLMClient(fail_mode="schema_fail")
    schema = {
        "type": "object",
        "required": ["needed"],
        "properties": {"needed": {"type": "string"}},
    }
    req = LLMRequest(
        system_prompt="x", user_prompt="y",
        response_schema=schema,
    )
    resp = client.invoke(req)
    assert not resp.schema_validated
    assert not resp.is_success
    print("✅ test_mock_schema_fail_mode")


def test_mock_exception_mode():
    client = MockLLMClient(fail_mode="exception")
    req = LLMRequest(system_prompt="x", user_prompt="y")
    try:
        client.invoke(req)
        assert False
    except RuntimeError:
        pass
    print("✅ test_mock_exception_mode")


def test_mock_call_history():
    client = MockLLMClient()
    for i in range(3):
        client.invoke(LLMRequest(
            system_prompt="x", user_prompt=f"q{i}",
        ))
    assert len(client.call_history) == 3
    assert client.call_history[1].user_prompt == "q1"
    print("✅ test_mock_call_history")


def test_mock_model_id():
    client = MockLLMClient()
    assert client.model_id == "mock-llm-v1"
    print("✅ test_mock_model_id")


def test_mock_subclass_of_llmclient():
    """MockLLMClient는 LLMClient의 sub-class."""
    client = MockLLMClient()
    assert isinstance(client, LLMClient)
    print("✅ test_mock_subclass_of_llmclient")


# ─────────────────────────────────────────────────
# 실행
# ─────────────────────────────────────────────────

def _run_all() -> int:
    failed = 0
    tests = [
        test_request_basic, test_request_empty_rejected,
        test_request_invalid_max_tokens, test_request_invalid_temperature,
        test_response_basic, test_response_naive_datetime_rejected,
        test_response_is_success_false, test_response_to_dict_no_raw_text,
        test_parse_json_direct, test_parse_json_markdown_block,
        test_parse_json_markdown_no_lang, test_parse_json_braces_extraction,
        test_parse_json_nested, test_parse_json_string_with_braces,
        test_parse_json_empty, test_parse_json_unbalanced,
        test_parse_json_top_level_array_rejected,
        test_validate_basic_pass, test_validate_missing_required,
        test_validate_wrong_type,
        test_mock_canned, test_mock_schema_based,
        test_mock_parse_fail_mode, test_mock_schema_fail_mode,
        test_mock_exception_mode, test_mock_call_history,
        test_mock_model_id, test_mock_subclass_of_llmclient,
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
    print("Task 37 v0.1 — _llm_client 테스트")
    print("─" * 50)
    failed = _run_all()
    print("─" * 50)
    if failed == 0:
        print("✅ 모든 테스트 통과")
        sys.exit(0)
    else:
        print(f"❌ {failed}개 실패")
        sys.exit(1)
