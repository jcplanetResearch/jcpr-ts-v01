"""
레지스트리 로더 (Registry Loader)
==================================

JCPR Trading System - jcpr-ts-v01
Task 45 v0.1

YAML 파일 → RegistryFile (검증) → StrategyRegistry.

설계 원칙 (Design Principles):
    - yaml.safe_load 만 사용 (임의 객체 deserialization 차단)
    - 명확한 에러 메시지 — 라인 번호 포함
    - 환경변수 보간 미지원 (시크릿 의존 회피)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Union

import yaml
from pydantic import ValidationError

from .registry import StrategyRegistry
from .schema import RegistryFile


class RegistryLoadError(Exception):
    """레지스트리 로드 실패."""


# ─────────────────────────────────────────────────
# 로더 (Loader)
# ─────────────────────────────────────────────────

def load_registry(path: Union[str, Path]) -> StrategyRegistry:
    """
    YAML 파일에서 검증된 StrategyRegistry 로드.

    Args:
        path: strategy_registry.yaml 경로

    Returns:
        StrategyRegistry (검증·불변)

    Raises:
        RegistryLoadError: 파일/YAML/검증 실패 — 한 번 wrapping
    """
    p = Path(path)

    # ─── 파일 존재 확인 ────────────────────────
    if not p.exists():
        raise RegistryLoadError(f"registry file not found: {p}")
    if not p.is_file():
        raise RegistryLoadError(f"registry path is not a file: {p}")

    # ─── YAML 파싱 — safe_load만 ───────────────
    try:
        with p.open("r", encoding="utf-8") as f:
            raw: Any = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise RegistryLoadError(f"YAML parse error in {p}: {e}") from e
    except OSError as e:
        raise RegistryLoadError(f"cannot read {p}: {e}") from e

    if raw is None:
        raise RegistryLoadError(f"empty YAML file: {p}")
    if not isinstance(raw, dict):
        raise RegistryLoadError(
            f"YAML root must be a mapping, got {type(raw).__name__}"
        )

    # ─── Pydantic 검증 ─────────────────────────
    try:
        rf = RegistryFile(**raw)
    except ValidationError as e:
        # 사용자 친화적 메시지로 변환
        errors = []
        for err in e.errors():
            loc = ".".join(str(x) for x in err["loc"])
            msg = err["msg"]
            errors.append(f"  - {loc}: {msg}")
        joined = "\n".join(errors)
        raise RegistryLoadError(
            f"validation failed for {p}:\n{joined}"
        ) from e

    return StrategyRegistry.from_registry_file(rf)


def load_registry_from_string(content: str) -> StrategyRegistry:
    """
    YAML 문자열에서 로드 — 테스트 편의용.

    Raises:
        RegistryLoadError
    """
    try:
        raw = yaml.safe_load(content)
    except yaml.YAMLError as e:
        raise RegistryLoadError(f"YAML parse error: {e}") from e

    if raw is None:
        raise RegistryLoadError("empty YAML content")
    if not isinstance(raw, dict):
        raise RegistryLoadError(
            f"YAML root must be a mapping, got {type(raw).__name__}"
        )

    try:
        rf = RegistryFile(**raw)
    except ValidationError as e:
        errors = []
        for err in e.errors():
            loc = ".".join(str(x) for x in err["loc"])
            errors.append(f"  - {loc}: {err['msg']}")
        joined = "\n".join(errors)
        raise RegistryLoadError(f"validation failed:\n{joined}") from e

    return StrategyRegistry.from_registry_file(rf)
