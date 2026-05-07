"""
전략 패키지 (Strategies Package)
================================

JCPR Trading System - jcpr-ts-v01
Task 45 v0.1 — Multi-Strategy Registry

다중 전략 등록·관리 인프라.
(Multi-strategy registration and management infrastructure.)

사용 (Usage):
    from src.strategies import load_registry

    registry = load_registry("configs/strategy_registry.yaml")

    # 활성 전략 순회
    for entry in registry.list_active():
        print(f"{entry.strategy_id} v{entry.version} on {entry.timeframe}")

    # 라이브 적격 전략 (enabled=True AND paper_only=False)
    for entry in registry.list_live_eligible():
        cls = entry.load_class()
        strategy = cls(**entry.parameters)

설계 (Design):
    - YAML로 전략 메타데이터 관리
    - Pydantic 엄격 검증 (시크릿 차단, 화이트리스트 module_path)
    - paper_only 우선 — 실수 라이브 차단
    - 자본 가중치 합 ≤ 1.0 검증
    - 동적 클래스 로더 (선택적, 화이트리스트 검증)
"""

from .capital_allocator import (
    ALLOWED_MODES,
    MODE_LIVE,
    MODE_PAPER,
    CapitalAllocation,
    StrategyAllocation,
    allocate_capital,
)
from .loader import (
    RegistryLoadError,
    load_registry,
    load_registry_from_string,
)
from .registry import StrategyRegistry
from .schema import (
    ALLOWED_MODULE_PREFIXES,
    ALLOWED_SIGNAL_CATEGORIES,
    ALLOWED_TIMEFRAMES,
    RegistryFile,
    StrategyEntry,
)

__all__ = [
    # 모델 (Task 45)
    "StrategyEntry",
    "RegistryFile",
    "StrategyRegistry",
    # 로더 (Task 45)
    "load_registry",
    "load_registry_from_string",
    "RegistryLoadError",
    # 상수 (Task 45)
    "ALLOWED_MODULE_PREFIXES",
    "ALLOWED_TIMEFRAMES",
    "ALLOWED_SIGNAL_CATEGORIES",
    # 자본 할당 (Task 46)
    "allocate_capital",
    "CapitalAllocation",
    "StrategyAllocation",
    "MODE_LIVE",
    "MODE_PAPER",
    "ALLOWED_MODES",
]

__version__ = "0.2.0"  # Task 46 추가
