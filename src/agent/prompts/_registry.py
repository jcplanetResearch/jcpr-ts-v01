"""
프롬프트 레지스트리 (Prompt Registry)
=======================================

JCPR Trading System - jcpr-ts-v01
Task 36 v0.1

파일 시스템에서 PromptTemplate 로드 + 캐싱.
(Loads PromptTemplate from filesystem with caching.)

저장 형식 (Storage Format):
    src/agents/prompts/system/<name>.md     ← system prompts
    src/agents/prompts/user/<name>.md       ← user task prompts
    src/agents/prompts/tools/<name>.md      ← tool guides
    src/agents/prompts/schemas/<name>.json  ← response schemas

각 .md 파일은 YAML frontmatter + body:
    ---
    template_id: market_analyst.system
    version: v1.0
    role: system
    target_agent: market_analyst
    description: "Market analysis system prompt"
    response_schema_path: schemas/market_analysis.json   # optional
    required_variables: []                                # optional
    ---
    
    Body content here with {{ variable_name }} placeholders...
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from ._template import (
    ALLOWED_AGENTS,
    ALLOWED_ROLES,
    PromptTemplate,
    extract_variables,
)


# ─────────────────────────────────────────────────
# 상수
# ─────────────────────────────────────────────────

# 기본 prompt root — repo 루트에서 src/agents/prompts/
DEFAULT_PROMPT_ROOT = Path(__file__).resolve().parent

# Frontmatter 구분자
FRONTMATTER_PATTERN = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n(.*)$",
    re.DOTALL,
)

# 보안: prompt root 이외 경로 차단
PATH_TRAVERSAL_PATTERN = re.compile(r"\.\.")


# ─────────────────────────────────────────────────
# 예외
# ─────────────────────────────────────────────────

class RegistryError(Exception):
    """레지스트리 오류."""


class TemplateNotFound(RegistryError):
    """템플릿 못 찾음."""


class TemplateLoadError(RegistryError):
    """템플릿 로드 실패 (형식 오류 등)."""


# ─────────────────────────────────────────────────
# 레지스트리
# ─────────────────────────────────────────────────

@dataclass
class PromptRegistry:
    """
    프롬프트 템플릿 레지스트리.

    Args:
        prompt_root: 프롬프트 루트 디렉터리
        cache_enabled: True면 한 번 로드한 템플릿 메모리 캐시
    """

    prompt_root: Path = field(default_factory=lambda: DEFAULT_PROMPT_ROOT)
    cache_enabled: bool = True
    _cache: dict[str, PromptTemplate] = field(
        default_factory=dict, init=False, repr=False,
    )
    _lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False,
    )

    def __post_init__(self):
        if isinstance(self.prompt_root, str):
            object.__setattr__(self, "prompt_root", Path(self.prompt_root))
        if not self.prompt_root.exists():
            raise RegistryError(
                f"prompt_root does not exist: {self.prompt_root}"
            )
        if not self.prompt_root.is_dir():
            raise RegistryError(
                f"prompt_root is not a directory: {self.prompt_root}"
            )

    # ─────────────────────────────────────────
    # 단일 조회
    # ─────────────────────────────────────────

    def get(self, template_id: str) -> PromptTemplate:
        """
        template_id로 조회. 캐시 우선.

        Args:
            template_id: "market_analyst.system" 등

        Raises:
            TemplateNotFound
            TemplateLoadError
        """
        if not isinstance(template_id, str) or "." not in template_id:
            raise RegistryError(
                f"template_id must be 'agent.name' format, got {template_id!r}"
            )

        # 캐시
        if self.cache_enabled:
            with self._lock:
                if template_id in self._cache:
                    return self._cache[template_id]

        # 파일에서 로드
        tmpl = self._load_by_id(template_id)

        if self.cache_enabled:
            with self._lock:
                self._cache[template_id] = tmpl
        return tmpl

    # ─────────────────────────────────────────
    # 일괄 조회
    # ─────────────────────────────────────────

    def list_all(self) -> list[PromptTemplate]:
        """모든 템플릿 로드 — CI/검증용. 메모리 사용량 큼."""
        all_templates: list[PromptTemplate] = []
        seen_ids: set[str] = set()

        for md_path in self.prompt_root.rglob("*.md"):
            if md_path.is_file():
                try:
                    tmpl = self._load_from_file(md_path)
                    if tmpl.template_id in seen_ids:
                        raise TemplateLoadError(
                            f"duplicate template_id: {tmpl.template_id}"
                        )
                    seen_ids.add(tmpl.template_id)
                    all_templates.append(tmpl)
                except TemplateLoadError:
                    raise
                except Exception as e:
                    raise TemplateLoadError(
                        f"failed to load {md_path}: {type(e).__name__}: {e}"
                    ) from e
        return sorted(all_templates, key=lambda t: t.template_id)

    def list_by_agent(self, target_agent: str) -> list[PromptTemplate]:
        """특정 agent의 템플릿."""
        if target_agent not in ALLOWED_AGENTS:
            raise RegistryError(
                f"target_agent invalid — allowed: {ALLOWED_AGENTS}"
            )
        return [t for t in self.list_all() if t.target_agent == target_agent]

    # ─────────────────────────────────────────
    # 캐시 관리
    # ─────────────────────────────────────────

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()

    # ─────────────────────────────────────────
    # 내부 (Private)
    # ─────────────────────────────────────────

    def _load_by_id(self, template_id: str) -> PromptTemplate:
        """template_id로 파일 검색 + 로드."""
        # 모든 .md 파일에서 frontmatter의 template_id 매칭 검색
        for md_path in self.prompt_root.rglob("*.md"):
            if not md_path.is_file():
                continue
            try:
                tmpl = self._load_from_file(md_path)
                if tmpl.template_id == template_id:
                    return tmpl
            except TemplateLoadError:
                # 단일 파일 오류는 무시 (검색 계속)
                continue
        raise TemplateNotFound(
            f"template_id '{template_id}' not found under {self.prompt_root}"
        )

    def _load_from_file(self, md_path: Path) -> PromptTemplate:
        """단일 .md 파일 → PromptTemplate."""
        # 경로 보안 — prompt_root 밖이면 거부
        try:
            md_path.relative_to(self.prompt_root)
        except ValueError as e:
            raise TemplateLoadError(
                f"path {md_path} outside prompt_root"
            ) from e
        if PATH_TRAVERSAL_PATTERN.search(str(md_path)):
            raise TemplateLoadError(f"path traversal in {md_path}")

        try:
            content = md_path.read_text(encoding="utf-8")
        except OSError as e:
            raise TemplateLoadError(f"read failed {md_path}: {e}") from e

        # frontmatter 분리
        m = FRONTMATTER_PATTERN.match(content)
        if not m:
            raise TemplateLoadError(
                f"{md_path}: missing YAML frontmatter (--- ... ---)"
            )

        fm_text, body = m.group(1), m.group(2)

        try:
            fm = yaml.safe_load(fm_text)
        except yaml.YAMLError as e:
            raise TemplateLoadError(f"{md_path}: YAML error: {e}") from e

        if not isinstance(fm, dict):
            raise TemplateLoadError(
                f"{md_path}: frontmatter must be dict"
            )

        # 필수 필드 추출
        try:
            template_id = str(fm["template_id"]).strip()
            version = str(fm["version"]).strip()
            role = str(fm["role"]).strip()
            target_agent = str(fm["target_agent"]).strip()
        except KeyError as e:
            raise TemplateLoadError(
                f"{md_path}: missing required frontmatter key {e}"
            ) from e

        description = str(fm.get("description", "")).strip()
        declared_vars = fm.get("required_variables", [])
        if not isinstance(declared_vars, list):
            raise TemplateLoadError(
                f"{md_path}: required_variables must be list"
            )
        declared_vars = [str(v).strip() for v in declared_vars]

        # response_schema 로드 (있으면)
        response_schema: Optional[dict] = None
        schema_path_str = fm.get("response_schema_path")
        if schema_path_str:
            schema_path = self.prompt_root / str(schema_path_str)
            try:
                schema_path.relative_to(self.prompt_root)
            except ValueError:
                raise TemplateLoadError(
                    f"{md_path}: schema path outside prompt_root: {schema_path_str}"
                )
            if not schema_path.exists():
                raise TemplateLoadError(
                    f"{md_path}: schema file not found: {schema_path}"
                )
            try:
                response_schema = json.loads(
                    schema_path.read_text(encoding="utf-8")
                )
            except json.JSONDecodeError as e:
                raise TemplateLoadError(
                    f"{schema_path}: JSON error: {e}"
                ) from e
            if not isinstance(response_schema, dict):
                raise TemplateLoadError(
                    f"{schema_path}: must be JSON object at root"
                )

        # body의 변수 + 선언된 변수 합집합
        body_vars = set(extract_variables(body.strip()))
        all_vars = sorted(body_vars | set(declared_vars))

        # PromptTemplate 생성 (검증은 __post_init__에서)
        try:
            return PromptTemplate(
                template_id=template_id,
                version=version,
                role=role,
                body=body.strip(),
                required_variables=tuple(all_vars),
                response_schema=response_schema,
                target_agent=target_agent,
                description=description,
                source_path=str(md_path.relative_to(self.prompt_root)),
            )
        except ValueError as e:
            raise TemplateLoadError(f"{md_path}: {e}") from e


# ─────────────────────────────────────────────────
# 글로벌 기본 레지스트리 (Default Registry)
# ─────────────────────────────────────────────────

_DEFAULT_REGISTRY: Optional[PromptRegistry] = None
_DEFAULT_LOCK = threading.Lock()


def get_default_registry() -> PromptRegistry:
    """기본 레지스트리 — lazy 초기화."""
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        with _DEFAULT_LOCK:
            if _DEFAULT_REGISTRY is None:
                _DEFAULT_REGISTRY = PromptRegistry()
    return _DEFAULT_REGISTRY


def reset_default_registry() -> None:
    """기본 레지스트리 리셋 — 테스트용."""
    global _DEFAULT_REGISTRY
    with _DEFAULT_LOCK:
        _DEFAULT_REGISTRY = None


__all__ = [
    "PromptRegistry",
    "RegistryError",
    "TemplateNotFound",
    "TemplateLoadError",
    "get_default_registry",
    "reset_default_registry",
    "DEFAULT_PROMPT_ROOT",
]
