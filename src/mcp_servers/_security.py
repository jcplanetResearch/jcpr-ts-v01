"""
보안 헬퍼 (Security Helpers)
==============================

JCPR Trading System - jcpr-ts-v01
Task 34 v0.1

MCP read-only 서버의 보안 계층:
    1. Rate limiter (token bucket)
    2. 출력 자동 마스킹 (Task A2 _mask_payload 재사용)
    3. 입력 검증 (정규식 + 길이 한도)
    4. PII 차단 (운영자 ID, 계좌번호 등)

설계 (Design):
    - MCP 호출 전후로 통과하는 게이트
    - 시크릿/PII는 절대 LLM에 노출 안 됨
    - rate limit 초과 시 명확한 에러
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional

# Task A2 마스킹 재사용 (defense-in-depth)
from src.observability.audit_writer import _mask_payload


# ─────────────────────────────────────────────────
# 입력 검증 정규식 (Input Validation Patterns)
# ─────────────────────────────────────────────────

SYMBOL_PATTERN = re.compile(r"^[A-Z0-9._\-]{1,16}$")
TRACE_ID_PATTERN = re.compile(r"^trc-\d{8}-[a-f0-9]{8,16}$")
SESSION_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_\-]{2,64}$")
ISO_DATETIME_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+\-]\d{2}:\d{2})?$"
)

# PII로 간주되는 키 (출력에서 제거)
PII_KEYS = (
    "operator_id_full",       # 풀 식별자 (anonymized 'op_xxx'만 노출)
    "account_number",
    "account_id_full",
    "phone",
    "email",
    "ssn",
    "personal_name",
    "ip_address",
    "user_agent",
)


# ─────────────────────────────────────────────────
# Rate Limiter (Token Bucket)
# ─────────────────────────────────────────────────

@dataclass
class RateLimiter:
    """
    분당 호출 한도 — 단순 sliding window.

    Args:
        max_per_minute: 분당 최대 호출
    """
    max_per_minute: int = 120
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _calls: deque = field(default_factory=deque, init=False, repr=False)

    def __post_init__(self):
        if self.max_per_minute <= 0:
            raise ValueError(f"max_per_minute must be > 0, got {self.max_per_minute}")

    def check(self) -> tuple[bool, Optional[str]]:
        """
        호출 허용 여부 + 에러 메시지.

        Returns:
            (True, None) if allowed
            (False, error_msg) if denied
        """
        now = time.monotonic()
        cutoff = now - 60.0

        with self._lock:
            # 60초 이전 항목 제거
            while self._calls and self._calls[0] < cutoff:
                self._calls.popleft()

            if len(self._calls) >= self.max_per_minute:
                wait_until = self._calls[0] + 60.0
                wait_s = max(0, wait_until - now)
                return False, (
                    f"Rate limit 초과 — 분당 {self.max_per_minute}회 한도. "
                    f"{wait_s:.1f}초 후 재시도."
                )

            self._calls.append(now)
            return True, None

    def current_count(self) -> int:
        """현재 분 호출 수 — 모니터링용."""
        now = time.monotonic()
        cutoff = now - 60.0
        with self._lock:
            while self._calls and self._calls[0] < cutoff:
                self._calls.popleft()
            return len(self._calls)


# ─────────────────────────────────────────────────
# 입력 검증 (Input Validation)
# ─────────────────────────────────────────────────

def validate_symbol(symbol: Optional[str]) -> Optional[str]:
    """심볼 형식 검증."""
    if symbol is None:
        return None
    if not isinstance(symbol, str):
        raise ValueError(f"symbol must be str, got {type(symbol).__name__}")
    symbol = symbol.strip().upper()
    if not SYMBOL_PATTERN.match(symbol):
        raise ValueError(
            f"symbol '{symbol}' 형식 오류 — alphanumeric/_/. 만 허용 (1-16자)"
        )
    return symbol


def validate_trace_id(trace_id: Optional[str]) -> Optional[str]:
    """trace_id 형식 검증."""
    if trace_id is None:
        return None
    if not isinstance(trace_id, str):
        raise ValueError(f"trace_id must be str")
    if not TRACE_ID_PATTERN.match(trace_id):
        raise ValueError(
            f"trace_id '{trace_id}' 형식 오류 — 'trc-YYYYMMDD-XXXXXXXX'"
        )
    return trace_id


def validate_iso_datetime(s: Optional[str]) -> Optional[str]:
    """ISO 8601 datetime 형식 검증."""
    if s is None:
        return None
    if not isinstance(s, str):
        raise ValueError(f"datetime must be str")
    if not ISO_DATETIME_PATTERN.match(s):
        raise ValueError(f"datetime '{s}' must be ISO 8601 format")
    return s


def validate_limit(value: Optional[int], *, default: int, max_value: int) -> int:
    """양수 정수 한도 검증."""
    if value is None:
        return default
    if not isinstance(value, int):
        raise ValueError(f"limit must be int, got {type(value).__name__}")
    if value < 1:
        raise ValueError(f"limit must be ≥ 1, got {value}")
    if value > max_value:
        raise ValueError(f"limit {value} > max {max_value}")
    return value


def validate_sector_map(d: Optional[dict]) -> dict[str, str]:
    """
    sector_map 검증 — {symbol: sector_str} 형식.

    Returns:
        검증된 dict (빈 dict 가능)
    """
    if d is None:
        return {}
    if not isinstance(d, dict):
        raise ValueError(f"sector_map must be dict, got {type(d).__name__}")
    if len(d) > 1000:
        raise ValueError(f"sector_map too large: {len(d)} entries (max 1000)")

    out: dict[str, str] = {}
    for k, v in d.items():
        sym = validate_symbol(k)
        if not isinstance(v, str):
            raise ValueError(f"sector value for {k!r} must be str")
        if len(v) > 32:
            raise ValueError(f"sector name too long: {v!r}")
        # sector 자체에 시크릿/PII 없는지 (단순 알파넷)
        if not re.match(r"^[a-zA-Z0-9_\-]+$", v):
            raise ValueError(f"sector '{v}' invalid characters")
        out[sym] = v
    return out


# ─────────────────────────────────────────────────
# 출력 마스킹 (Output Masking)
# ─────────────────────────────────────────────────

def mask_output(data: Any) -> Any:
    """
    MCP 응답 마스킹 — Task A2 마스킹 + PII 추가.

    Args:
        data: 도구가 반환할 dict/list

    Returns:
        마스킹된 동일 구조
    """
    # 1. Task A2 시크릿 마스킹 (재귀)
    masked = _mask_payload(data)
    # 2. PII 추가 마스킹
    masked = _mask_pii(masked)
    # 3. Decimal → str (LLM이 읽기 좋음)
    masked = _decimal_to_str(masked)
    return masked


def _mask_pii(data: Any) -> Any:
    """PII 키 마스킹."""
    if isinstance(data, dict):
        out: dict[str, Any] = {}
        for k, v in data.items():
            low = str(k).lower()
            if any(pii in low for pii in PII_KEYS):
                out[k] = "***PII_MASKED***"
            else:
                out[k] = _mask_pii(v)
        return out
    if isinstance(data, (list, tuple)):
        return [_mask_pii(item) for item in data]
    return data


def _decimal_to_str(data: Any) -> Any:
    """Decimal → str (LLM 안전)."""
    if isinstance(data, Decimal):
        return str(data)
    if isinstance(data, dict):
        return {k: _decimal_to_str(v) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return [_decimal_to_str(item) for item in data]
    return data


# ─────────────────────────────────────────────────
# 결과 크기 제한 (Result Size Limit)
# ─────────────────────────────────────────────────

MAX_RESULT_BYTES = 256_000   # 256KB — LLM context 부담 방지


def check_result_size(serialized: str) -> tuple[bool, Optional[str]]:
    """직렬화된 결과 크기 검증."""
    size = len(serialized.encode("utf-8"))
    if size > MAX_RESULT_BYTES:
        return False, (
            f"결과 크기 {size:,} bytes > 한도 {MAX_RESULT_BYTES:,} — "
            f"limit/since_iso 사용해 범위 좁히세요"
        )
    return True, None
