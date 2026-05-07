"""
LLM 클라이언트 (LLM Client)
=============================

JCPR Trading System - jcpr-ts-v01
Task 37 v0.1

LLM-agnostic 클라이언트 추상 인터페이스 + 테스트용 Mock.
(LLM-agnostic client interface + Mock for testing.)

설계 원칙 (Design Principles):
    - LLMClient 추상 — Tasks 37/38/39 모두 재사용
    - 외부 SDK 의존 0 — 운영 시 별도 어댑터 추가 가능
    - 입력은 (system_prompt, user_prompt, response_schema)
    - 출력은 LLMResponse (rendered_text + parsed_json + 메타)
    - MockLLMClient는 schema 기반 fixture 응답 생성

비-목표 (Non-goals):
    - 실제 LLM API 호출 (별도 AnthropicClient 등 어댑터에서 처리)
    - 스트리밍 (단발 호출만 — 거래 시스템 특성상)
    - Function calling (도구 호출은 agent_runner가 직접 처리)
"""

from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


logger = logging.getLogger("jcpr.agents.llm")


# ─────────────────────────────────────────────────
# 데이터 모델 (Data Models)
# ─────────────────────────────────────────────────

@dataclass(frozen=True)
class LLMRequest:
    """LLM 요청."""

    system_prompt: str
    user_prompt: str
    response_schema: Optional[dict[str, Any]] = None
    max_tokens: int = 4096
    temperature: float = 0.0   # 결정론적 (거래 시스템)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.system_prompt or not self.user_prompt:
            raise ValueError("system_prompt and user_prompt required")
        if self.max_tokens < 1 or self.max_tokens > 32768:
            raise ValueError(f"max_tokens out of range: {self.max_tokens}")
        if self.temperature < 0.0 or self.temperature > 2.0:
            raise ValueError(f"temperature out of range: {self.temperature}")


@dataclass(frozen=True)
class LLMResponse:
    """LLM 응답."""

    raw_text: str
    parsed_json: Optional[dict[str, Any]]
    parse_error: Optional[str]
    schema_validated: bool
    schema_error: Optional[str]
    model_id: str
    elapsed_ms: float
    received_at_utc: datetime
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.received_at_utc.tzinfo is None:
            raise ValueError("received_at_utc must be tz-aware")

    @property
    def is_success(self) -> bool:
        """파싱 성공 + (schema 없거나 validate 통과)."""
        return self.parsed_json is not None and self.schema_validated

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_text_length": len(self.raw_text),
            "parsed": self.parsed_json is not None,
            "parse_error": self.parse_error,
            "schema_validated": self.schema_validated,
            "schema_error": self.schema_error,
            "model_id": self.model_id,
            "elapsed_ms": self.elapsed_ms,
            "received_at_utc": self.received_at_utc.isoformat(),
            "is_success": self.is_success,
        }


# ─────────────────────────────────────────────────
# 추상 인터페이스 (Abstract Interface)
# ─────────────────────────────────────────────────

class LLMClient(ABC):
    """
    LLM 클라이언트 추상 — 모든 구현체는 invoke() 만 구현.

    Implementations:
        - MockLLMClient: 테스트용 (schema 기반 fixture 응답)
        - AnthropicClient (별도 모듈 — 운영 시): 실제 Claude API
        - OpenAIClient: ChatGPT API
        - GoogleGeminiClient: Gemini API
    """

    @abstractmethod
    def invoke(self, request: LLMRequest) -> LLMResponse:
        """단발 호출 — 응답 받기까지 동기 블로킹."""
        ...

    @property
    @abstractmethod
    def model_id(self) -> str:
        """식별자 — audit 기록용."""
        ...


# ─────────────────────────────────────────────────
# Schema 검증 (jsonschema)
# ─────────────────────────────────────────────────

def validate_response(
    parsed: dict[str, Any],
    schema: dict[str, Any],
) -> tuple[bool, Optional[str]]:
    """
    jsonschema로 검증.

    Returns:
        (validated, error_message)
    """
    try:
        from jsonschema import Draft202012Validator, ValidationError
    except ImportError:
        return False, "jsonschema 라이브러리 미설치"

    try:
        Draft202012Validator.check_schema(schema)
    except Exception as e:  # noqa: BLE001
        return False, f"schema 자체 오류: {e}"

    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(parsed), key=lambda e: e.path)
    if not errors:
        return True, None

    # 처음 3개 오류만 (간결성)
    msgs = []
    for err in errors[:3]:
        path = ".".join(str(p) for p in err.path) or "<root>"
        msgs.append(f"{path}: {err.message}")
    if len(errors) > 3:
        msgs.append(f"... ({len(errors) - 3}개 더)")
    return False, "; ".join(msgs)


def parse_json_response(raw_text: str) -> tuple[Optional[dict], Optional[str]]:
    """
    LLM 응답에서 JSON 파싱.

    LLM은 종종 ```json ... ``` 또는 설명 + JSON 형태로 응답하므로 robust 추출.

    Returns:
        (parsed_dict_or_None, error_message_or_None)
    """
    if not raw_text or not isinstance(raw_text, str):
        return None, "raw_text empty"

    # 1차: 그대로 파싱 시도
    text = raw_text.strip()
    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result, None
        return None, f"top-level not dict, got {type(result).__name__}"
    except json.JSONDecodeError:
        pass

    # 2차: ```json ... ``` 블록 추출
    md_match = re.search(
        r"```(?:json)?\s*\n?(.*?)\n?```",
        text,
        re.DOTALL,
    )
    if md_match:
        try:
            result = json.loads(md_match.group(1))
            if isinstance(result, dict):
                return result, None
        except json.JSONDecodeError:
            pass

    # 3차: 첫 { ... } 균형 맞춰 추출
    try:
        start = text.index("{")
    except ValueError:
        return None, "no opening brace found"

    depth = 0
    in_str = False
    escape = False
    end_idx = -1
    for i in range(start, len(text)):
        c = text[i]
        if escape:
            escape = False
            continue
        if c == "\\":
            escape = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end_idx = i
                break
    if end_idx == -1:
        return None, "unbalanced braces"

    try:
        result = json.loads(text[start:end_idx + 1])
        if isinstance(result, dict):
            return result, None
        return None, "extracted JSON not dict"
    except json.JSONDecodeError as e:
        return None, f"json decode error: {e}"


# ─────────────────────────────────────────────────
# MockLLMClient (테스트용)
# ─────────────────────────────────────────────────

@dataclass
class MockLLMClient(LLMClient):
    """
    테스트용 Mock — 미리 정의된 응답 또는 schema 기반 fixture.

    Args:
        canned_response: 명시 시 항상 이 응답 반환
        schema_based: True면 response_schema 기반 fixture 자동 생성
        fail_mode: 실패 모드 ("none", "parse_fail", "schema_fail", "exception")
    """

    canned_response: Optional[str] = None
    schema_based: bool = True
    fail_mode: str = "none"
    model_id_value: str = "mock-llm-v1"
    call_history: list[LLMRequest] = field(default_factory=list)

    @property
    def model_id(self) -> str:
        return self.model_id_value

    def invoke(self, request: LLMRequest) -> LLMResponse:
        self.call_history.append(request)

        now = datetime.now(timezone.utc)

        # 실패 시뮬레이션
        if self.fail_mode == "exception":
            raise RuntimeError("Mock LLM failure")

        if self.fail_mode == "parse_fail":
            return LLMResponse(
                raw_text="this is not json",
                parsed_json=None,
                parse_error="not json",
                schema_validated=False,
                schema_error=None,
                model_id=self.model_id,
                elapsed_ms=0.1,
                received_at_utc=now,
            )

        # 응답 생성
        if self.canned_response is not None:
            raw_text = self.canned_response
        elif self.schema_based and request.response_schema:
            raw_text = json.dumps(
                _generate_fixture_from_schema(request.response_schema),
                ensure_ascii=False,
            )
        else:
            raw_text = json.dumps({"response": "ok"}, ensure_ascii=False)

        # 파싱
        parsed, parse_err = parse_json_response(raw_text)

        # Schema 검증
        validated = True
        schema_err = None
        if request.response_schema and parsed is not None:
            validated, schema_err = validate_response(
                parsed, request.response_schema,
            )

        if self.fail_mode == "schema_fail" and parsed is not None:
            # 강제 schema 실패
            parsed = {"invalid": "structure"}
            validated = False
            schema_err = "forced schema_fail"

        return LLMResponse(
            raw_text=raw_text,
            parsed_json=parsed,
            parse_error=parse_err,
            schema_validated=validated,
            schema_error=schema_err,
            model_id=self.model_id,
            elapsed_ms=0.5,
            received_at_utc=now,
        )


# ─────────────────────────────────────────────────
# Schema → Fixture 자동 생성
# ─────────────────────────────────────────────────

def _generate_fixture_from_schema(schema: dict[str, Any]) -> Any:
    """간단한 schema → fixture (테스트용 — 모든 case 보장 안 함)."""
    if not isinstance(schema, dict):
        return None

    sch_type = schema.get("type", "object")

    if sch_type == "object":
        result = {}
        props = schema.get("properties", {})
        required = set(schema.get("required", []))
        for k, sub in props.items():
            if k in required or len(result) < 3:  # 필수 + 최소 3개
                result[k] = _generate_fixture_from_schema(sub)
        return result

    if sch_type == "array":
        items_schema = schema.get("items", {"type": "string"})
        max_items = schema.get("maxItems", 2)
        return [
            _generate_fixture_from_schema(items_schema)
            for _ in range(min(2, max_items))
        ]

    if sch_type == "string":
        if "enum" in schema:
            return schema["enum"][0]
        return "test_value"

    if sch_type == "integer":
        return 0

    if sch_type == "number":
        return 0.0

    if sch_type == "boolean":
        return True

    if sch_type == "null":
        return None

    # type이 list (multiple)인 경우
    if isinstance(sch_type, list):
        for t in sch_type:
            if t == "null":
                continue
            return _generate_fixture_from_schema({"type": t})
        return None

    return None


__all__ = [
    "LLMRequest",
    "LLMResponse",
    "LLMClient",
    "MockLLMClient",
    "validate_response",
    "parse_json_response",
]
