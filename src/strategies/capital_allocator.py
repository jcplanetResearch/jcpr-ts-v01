"""
전략별 자본 할당 (Strategy-Level Capital Allocation)
======================================================

JCPR Trading System - jcpr-ts-v01
Task 46 v0.1

Task 45 StrategyRegistry 기반으로 활성 전략에 자본 분배.
(Allocates capital to active strategies based on Task 45 registry.)

알고리즘 (Algorithm):
    1. 후보 선별 (Filter) — mode에 따라 live/paper
    2. 비례 분배 (Proportional)
    3. 한도 캡 + 자동 재분배 (Cap & Redistribute)
    4. 잔여 = 현금 버퍼 (Cash buffer)

설계 원칙 (Design Principles):
    - 모든 금액 Decimal — float 금지
    - 입력/결과 frozen=True 불변
    - read-only — Registry 변경 안 함
    - paper_only 분리 — live 모드에서 자동 제외
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Optional

from .registry import StrategyRegistry
from .schema import StrategyEntry


# ─────────────────────────────────────────────────
# 상수 (Constants)
# ─────────────────────────────────────────────────

# 모드 (Mode)
MODE_LIVE = "live"
MODE_PAPER = "paper"
ALLOWED_MODES = (MODE_LIVE, MODE_PAPER)

# Decimal 정밀도 (KRW 정수 단위)
KRW_QUANTUM = Decimal("1")

# 재분배 반복 한도 (무한루프 방지)
MAX_REDISTRIBUTION_ITERATIONS = 50

# 수치 오차 허용 (Decimal precision tolerance)
EPSILON = Decimal("0.01")  # 1전


# ─────────────────────────────────────────────────
# 데이터 모델 (Data Models)
# ─────────────────────────────────────────────────

@dataclass(frozen=True)
class StrategyAllocation:
    """단일 전략 할당 결과."""
    strategy_id: str
    capital_weight: Decimal       # registry에서 (참조)
    max_capital_pct: Decimal      # registry에서 (참조)
    allocated_krw: Decimal        # 실제 할당 KRW
    allocated_pct: Decimal        # allocated / total (0-1)
    capped: bool                  # max_capital_pct에 걸렸는지
    iterations_after_cap: int     # 캡 후 재분배 횟수

    def to_dict(self) -> dict:
        return {
            "strategy_id": self.strategy_id,
            "capital_weight": str(self.capital_weight),
            "max_capital_pct": str(self.max_capital_pct),
            "allocated_krw": str(self.allocated_krw),
            "allocated_pct": str(self.allocated_pct),
            "capped": self.capped,
            "iterations_after_cap": self.iterations_after_cap,
        }


@dataclass(frozen=True)
class CapitalAllocation:
    """전체 자본 할당 결과."""
    mode: str                                    # "live" | "paper"
    total_capital_krw: Decimal
    allocated_total_krw: Decimal                 # sum(allocated)
    cash_buffer_krw: Decimal
    cash_buffer_pct: Decimal
    allocations: tuple[StrategyAllocation, ...]
    excluded_strategies: tuple[str, ...]         # 제외된 전략 ID
    warnings: tuple[str, ...]
    redistribute_overflow: bool                  # 재분배 활성화 여부
    computed_at_utc: datetime

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "total_capital_krw": str(self.total_capital_krw),
            "allocated_total_krw": str(self.allocated_total_krw),
            "cash_buffer_krw": str(self.cash_buffer_krw),
            "cash_buffer_pct": str(self.cash_buffer_pct),
            "allocations": [a.to_dict() for a in self.allocations],
            "excluded_strategies": list(self.excluded_strategies),
            "warnings": list(self.warnings),
            "redistribute_overflow": self.redistribute_overflow,
            "computed_at_utc": self.computed_at_utc.isoformat(),
        }

    def to_json(self, *, indent: int = 2) -> str:
        import json
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def get(self, strategy_id: str) -> Optional[StrategyAllocation]:
        """ID로 조회."""
        for a in self.allocations:
            if a.strategy_id == strategy_id:
                return a
        return None

    def __repr__(self) -> str:
        return (
            f"CapitalAllocation(mode={self.mode!r}, "
            f"total={self.total_capital_krw}, "
            f"allocated={self.allocated_total_krw}, "
            f"cash={self.cash_buffer_krw}, "
            f"strategies={len(self.allocations)})"
        )


# ─────────────────────────────────────────────────
# 메인 함수 (Main Function)
# ─────────────────────────────────────────────────

def allocate_capital(
    registry: StrategyRegistry,
    total_capital_krw: Decimal,
    *,
    mode: str = MODE_LIVE,
    redistribute_overflow: bool = True,
) -> CapitalAllocation:
    """
    Task 45 레지스트리 기반 자본 할당.

    Args:
        registry: 검증된 StrategyRegistry
        total_capital_krw: 총 자본 (KRW)
        mode: "live" (live-eligible만) | "paper" (active 전체)
        redistribute_overflow: True면 캡 초과분 재분배

    Returns:
        CapitalAllocation (frozen)

    Raises:
        ValueError: 음수 자본, 잘못된 mode 등
    """
    # ─── 입력 검증 ─────────────────────────────
    total = Decimal(str(total_capital_krw))
    if total < 0:
        raise ValueError(f"total_capital_krw must be ≥ 0, got {total}")

    if mode not in ALLOWED_MODES:
        raise ValueError(
            f"mode must be one of {ALLOWED_MODES}, got {mode!r}"
        )

    warnings: list[str] = []

    # ─── 후보 선별 ─────────────────────────────
    if mode == MODE_LIVE:
        candidates = list(registry.list_live_eligible())
        # paper_only는 모두 제외 ID로
        excluded = tuple(
            e.strategy_id for e in registry.list_all()
            if e not in candidates
        )
    else:  # MODE_PAPER
        candidates = list(registry.list_active())
        excluded = tuple(
            e.strategy_id for e in registry.list_all()
            if not e.enabled
        )

    # ─── 빈 후보 처리 ──────────────────────────
    if not candidates:
        warnings.append(
            f"활성 전략 없음 (No active strategies for mode={mode}) — "
            f"전체 자본이 현금 버퍼로 분류됨"
        )
        return CapitalAllocation(
            mode=mode,
            total_capital_krw=total,
            allocated_total_krw=Decimal(0),
            cash_buffer_krw=total,
            cash_buffer_pct=Decimal(1) if total > 0 else Decimal(0),
            allocations=(),
            excluded_strategies=excluded,
            warnings=tuple(warnings),
            redistribute_overflow=redistribute_overflow,
            computed_at_utc=datetime.now(timezone.utc),
        )

    # ─── 가중치 합 검증 ────────────────────────
    weight_sum = sum(
        (e.capital_weight for e in candidates), Decimal(0)
    )
    if weight_sum > Decimal("1.0") + EPSILON:
        # 이미 RegistryFile에서 enabled 합 ≤ 1.0 검증되었으나
        # mode 필터 후 여전히 초과 가능 (paper 모드 등)
        raise ValueError(
            f"가중치 합 {weight_sum} > 1.0 — registry에 오류"
        )
    if weight_sum == 0:
        warnings.append(
            f"활성 전략 capital_weight 합이 0 — 전체 자본이 현금 버퍼"
        )
        return CapitalAllocation(
            mode=mode,
            total_capital_krw=total,
            allocated_total_krw=Decimal(0),
            cash_buffer_krw=total,
            cash_buffer_pct=Decimal(1) if total > 0 else Decimal(0),
            allocations=tuple(
                StrategyAllocation(
                    strategy_id=e.strategy_id,
                    capital_weight=e.capital_weight,
                    max_capital_pct=e.max_capital_pct,
                    allocated_krw=Decimal(0),
                    allocated_pct=Decimal(0),
                    capped=False,
                    iterations_after_cap=0,
                )
                for e in candidates
            ),
            excluded_strategies=excluded,
            warnings=tuple(warnings),
            redistribute_overflow=redistribute_overflow,
            computed_at_utc=datetime.now(timezone.utc),
        )

    # ─── 1단계: 비례 분배 (Proportional) ───────
    raw_alloc: dict[str, Decimal] = {
        e.strategy_id: total * e.capital_weight
        for e in candidates
    }

    # 캡 (Cap)
    cap_alloc: dict[str, Decimal] = {
        e.strategy_id: total * e.max_capital_pct
        for e in candidates
    }

    # 작업용 가변 dict
    final_alloc: dict[str, Decimal] = {}
    capped_ids: set[str] = set()
    iter_count: dict[str, int] = {e.strategy_id: 0 for e in candidates}

    # ─── 2단계: 캡 적용 ────────────────────────
    overflow = Decimal(0)
    for entry in candidates:
        sid = entry.strategy_id
        raw = raw_alloc[sid]
        cap = cap_alloc[sid]
        if raw > cap:
            overflow += (raw - cap)
            final_alloc[sid] = cap
            capped_ids.add(sid)
            warnings.append(
                f"전략 {sid}: 비례분배 {raw:.0f} > 캡 {cap:.0f} "
                f"→ 캡 적용, 초과 {raw - cap:.0f} 처리됨"
            )
        else:
            final_alloc[sid] = raw

    # ─── 3단계: 재분배 (Redistribute) ──────────
    if redistribute_overflow and overflow > EPSILON:
        # 캡에 안 걸린 전략들에게 비례 재분배
        for iteration in range(MAX_REDISTRIBUTION_ITERATIONS):
            if overflow <= EPSILON:
                break

            # 재분배 가능한 전략 = 아직 캡에 안 걸렸고 여유가 있는 전략
            eligible = [
                e for e in candidates
                if e.strategy_id not in capped_ids
                and final_alloc[e.strategy_id] < cap_alloc[e.strategy_id]
            ]
            if not eligible:
                # 재분배할 곳이 없음 — 잔여는 현금 버퍼로
                if overflow > EPSILON:
                    warnings.append(
                        f"재분배 불가 — 모든 전략 캡 도달, "
                        f"잔여 {overflow:.0f}는 현금 버퍼로"
                    )
                break

            # 재분배 가중치 합 (남은 후보의 capital_weight)
            redist_weight_sum = sum(
                (e.capital_weight for e in eligible), Decimal(0)
            )
            if redist_weight_sum == 0:
                # 모두 가중치 0 — 균등 분배로 fallback
                share = overflow / Decimal(len(eligible))
                shares = {e.strategy_id: share for e in eligible}
            else:
                shares = {
                    e.strategy_id: overflow * (e.capital_weight / redist_weight_sum)
                    for e in eligible
                }

            # 분배 적용 + 새 overflow 계산
            new_overflow = Decimal(0)
            for entry in eligible:
                sid = entry.strategy_id
                proposed = final_alloc[sid] + shares[sid]
                cap = cap_alloc[sid]
                if proposed > cap:
                    new_overflow += (proposed - cap)
                    final_alloc[sid] = cap
                    capped_ids.add(sid)
                else:
                    final_alloc[sid] = proposed
                iter_count[sid] += 1

            overflow = new_overflow

        else:
            # for-else: max iterations 도달
            warnings.append(
                f"재분배 최대 반복 {MAX_REDISTRIBUTION_ITERATIONS}회 도달 — "
                f"잔여 {overflow:.0f}는 현금 버퍼로"
            )
    elif not redistribute_overflow and overflow > EPSILON:
        warnings.append(
            f"redistribute_overflow=False — 캡 초과분 {overflow:.0f}는 "
            f"전부 현금 버퍼로"
        )

    # ─── 4단계: 정수 단위 quantize + 결과 조립 ─
    allocations: list[StrategyAllocation] = []
    allocated_total = Decimal(0)
    for entry in candidates:
        sid = entry.strategy_id
        amount = final_alloc[sid].quantize(KRW_QUANTUM, rounding=ROUND_DOWN)
        allocated_total += amount
        pct = (amount / total) if total > 0 else Decimal(0)
        allocations.append(StrategyAllocation(
            strategy_id=sid,
            capital_weight=entry.capital_weight,
            max_capital_pct=entry.max_capital_pct,
            allocated_krw=amount,
            allocated_pct=pct.quantize(Decimal("0.0001")),
            capped=sid in capped_ids,
            iterations_after_cap=iter_count[sid],
        ))

    cash_buffer = total - allocated_total
    cash_buffer_pct = (cash_buffer / total) if total > 0 else Decimal(0)

    # 무결성 검증 (Sanity check)
    if allocated_total > total + EPSILON:
        raise RuntimeError(
            f"내부 오류: 할당 합 {allocated_total} > 총자본 {total}"
        )
    if cash_buffer < -EPSILON:
        raise RuntimeError(
            f"내부 오류: 음수 현금 버퍼 {cash_buffer}"
        )

    return CapitalAllocation(
        mode=mode,
        total_capital_krw=total,
        allocated_total_krw=allocated_total,
        cash_buffer_krw=cash_buffer,
        cash_buffer_pct=cash_buffer_pct.quantize(Decimal("0.0001")),
        allocations=tuple(allocations),
        excluded_strategies=excluded,
        warnings=tuple(warnings),
        redistribute_overflow=redistribute_overflow,
        computed_at_utc=datetime.now(timezone.utc),
    )
