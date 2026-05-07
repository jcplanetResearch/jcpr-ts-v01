"""
전략 레지스트리 (Strategy Registry)
====================================

JCPR Trading System - jcpr-ts-v01
Task 45 v0.1

검증된 StrategyEntry 모음 + 조회 헬퍼.
(Validated collection of StrategyEntry + query helpers.)

설계 원칙 (Design Principles):
    - 불변 (immutable) — 로드 후 변경 불가
    - 화이트리스트 우선 — paper_only/enabled 필터로 위험 차단
    - 자본 가중치 합 검증
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterator, Optional

from .schema import RegistryFile, StrategyEntry


@dataclass(frozen=True)
class StrategyRegistry:
    """
    검증된 전략 레지스트리.

    사용 (Usage):
        registry = load_registry("configs/strategy_registry.yaml")
        for entry in registry.list_active():
            print(entry.strategy_id)

        entry = registry.get("momentum_v1")
        if entry and not entry.paper_only:
            cls = entry.load_class()
            strategy = cls(**entry.parameters)
    """

    file_version: str
    entries: tuple[StrategyEntry, ...]

    # ─────────────────────────────────────────
    # 팩토리 (Factory)
    # ─────────────────────────────────────────

    @classmethod
    def from_registry_file(cls, rf: RegistryFile) -> "StrategyRegistry":
        """RegistryFile에서 생성."""
        return cls(
            file_version=rf.version,
            entries=tuple(rf.strategies),
        )

    # ─────────────────────────────────────────
    # 조회 (Query)
    # ─────────────────────────────────────────

    def __iter__(self) -> Iterator[StrategyEntry]:
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def __contains__(self, strategy_id: str) -> bool:
        return any(e.strategy_id == strategy_id for e in self.entries)

    def get(self, strategy_id: str) -> Optional[StrategyEntry]:
        """ID로 조회 — 없으면 None."""
        for e in self.entries:
            if e.strategy_id == strategy_id:
                return e
        return None

    def require(self, strategy_id: str) -> StrategyEntry:
        """ID로 조회 — 없으면 KeyError."""
        e = self.get(strategy_id)
        if e is None:
            raise KeyError(f"strategy_id {strategy_id!r} not in registry")
        return e

    def list_all(self) -> tuple[StrategyEntry, ...]:
        """전체 (활성/비활성 포함)."""
        return self.entries

    def list_active(self) -> tuple[StrategyEntry, ...]:
        """`enabled=True` 만."""
        return tuple(e for e in self.entries if e.enabled)

    def list_paper_only(self) -> tuple[StrategyEntry, ...]:
        """`paper_only=True` 만 (활성 여부 무관)."""
        return tuple(e for e in self.entries if e.paper_only)

    def list_live_eligible(self) -> tuple[StrategyEntry, ...]:
        """라이브 실행 가능: enabled=True AND paper_only=False."""
        return tuple(
            e for e in self.entries
            if e.enabled and not e.paper_only
        )

    def list_by_timeframe(self, timeframe: str) -> tuple[StrategyEntry, ...]:
        """timeframe 일치 전략."""
        return tuple(e for e in self.entries if e.timeframe == timeframe)

    def list_by_symbol(self, symbol: str) -> tuple[StrategyEntry, ...]:
        """
        심볼이 universe에 포함된 전략.

        주의: universe가 빈 리스트인 전략은 "전체 허용"으로 간주.
        """
        return tuple(
            e for e in self.entries
            if not e.universe or symbol in e.universe
        )

    # ─────────────────────────────────────────
    # 자본 가중치 (Capital Weights)
    # ─────────────────────────────────────────

    def total_capital_weight(self, *, active_only: bool = True) -> Decimal:
        """capital_weight 합 (기본: 활성 전략만)."""
        target = self.list_active() if active_only else self.entries
        return sum((e.capital_weight for e in target), Decimal(0))

    def is_paper_only(self, strategy_id: str) -> bool:
        """ID가 페이퍼 전용인지 — 미등록은 True (안전)."""
        e = self.get(strategy_id)
        if e is None:
            return True
        return e.paper_only

    # ─────────────────────────────────────────
    # 표시 (Display)
    # ─────────────────────────────────────────

    def summary(self) -> dict:
        """레지스트리 요약 — 대시보드/리포트용."""
        return {
            "file_version": self.file_version,
            "total_strategies": len(self.entries),
            "active_count": len(self.list_active()),
            "paper_only_count": len(self.list_paper_only()),
            "live_eligible_count": len(self.list_live_eligible()),
            "total_active_capital_weight": str(self.total_capital_weight()),
            "by_timeframe": self._count_by_attr("timeframe"),
            "strategies": [
                {
                    "strategy_id": e.strategy_id,
                    "version": e.version,
                    "enabled": e.enabled,
                    "paper_only": e.paper_only,
                    "timeframe": e.timeframe,
                    "capital_weight": str(e.capital_weight),
                    "universe_size": len(e.universe),
                    "categories": e.signal_categories,
                }
                for e in self.entries
            ],
        }

    def _count_by_attr(self, attr: str) -> dict:
        """속성값 별 카운트."""
        counts: dict[str, int] = {}
        for e in self.entries:
            v = str(getattr(e, attr))
            counts[v] = counts.get(v, 0) + 1
        return counts

    def __repr__(self) -> str:
        return (
            f"StrategyRegistry(file_version={self.file_version!r}, "
            f"total={len(self.entries)}, "
            f"active={len(self.list_active())}, "
            f"live_eligible={len(self.list_live_eligible())})"
        )
