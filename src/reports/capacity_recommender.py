"""
다음 세션 자본 추천 (Next Session Capacity Recommender)
========================================================

JCPR Trading System - jcpr-ts-v01
Task 49 v0.2 — Final Output #12

5단계 시그널 강도 기반 보수적 추천.
(5-stage signal-strength-based conservative recommendation.)

원칙 (Principles):
    - 안전 우선 (safety first) — 문제 발생 시 capacity 축소
    - 운영자 결정 보조 (advisory) — 자동 실행 안 함
    - 임계값 외부 주입 가능 (thresholds injectable for testing)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional


# ─────────────────────────────────────────────────
# 임계값 (Thresholds — injectable)
# ─────────────────────────────────────────────────

@dataclass(frozen=True)
class CapacityThresholds:
    """추천 임계값 — 외부 주입 가능."""
    # 신호 카운트 임계값
    rejection_rate_high: float = 0.30      # >30% 거부율 → 신호
    rejection_rate_critical: float = 0.50  # >50% 거부율 → 강한 신호
    exception_count_warn: int = 1          # 1건 이상 예외 → 신호
    exception_count_critical: int = 5      # 5건 이상 → 강한 신호
    pnl_loss_pct_warn: float = -0.02       # -2% 손실 → 신호
    pnl_loss_pct_critical: float = -0.05   # -5% 손실 → 강한 신호

    # capacity 조정 비율 (배수)
    multiplier_increase: Decimal = Decimal("1.10")    # +10% (긍정 시)
    multiplier_hold: Decimal = Decimal("1.00")        # 유지
    multiplier_reduce_mild: Decimal = Decimal("0.80")  # -20%
    multiplier_reduce_strong: Decimal = Decimal("0.50")  # -50%
    multiplier_halt: Decimal = Decimal("0.00")         # 중단


DEFAULT_THRESHOLDS = CapacityThresholds()


# ─────────────────────────────────────────────────
# 추천 결과 (Recommendation Result)
# ─────────────────────────────────────────────────

@dataclass(frozen=True)
class CapacityRecommendation:
    """다음 세션 자본 추천 결과."""
    current_capital_krw: str           # Decimal as string
    recommended_capital_krw: str       # Decimal as string
    multiplier: str                    # Decimal as string
    stage: str                         # "increase", "hold", "reduce_mild", "reduce_strong", "halt"
    risk_signals: int                  # 감지된 신호 수
    signal_details: list[str] = field(default_factory=list)
    reasoning: str = ""

    def to_dict(self) -> dict:
        return {
            "current_capital_krw": self.current_capital_krw,
            "recommended_capital_krw": self.recommended_capital_krw,
            "multiplier": self.multiplier,
            "stage": self.stage,
            "risk_signals": self.risk_signals,
            "signal_details": self.signal_details,
            "reasoning": self.reasoning,
        }


# ─────────────────────────────────────────────────
# 5단계 추천 알고리즘 (5-Stage Algorithm)
# ─────────────────────────────────────────────────

def recommend_next_capacity(
    *,
    starting_capital_krw: Decimal,
    realized_pnl_krw: Decimal,
    unrealized_pnl_krw: Decimal,
    rejection_rate: float = 0.0,
    exception_count: int = 0,
    reconciliation_severity: str = "ok",   # "ok" / "minor" / "major"
    portfolio_risk_warnings: int = 0,
    thresholds: Optional[CapacityThresholds] = None,
) -> CapacityRecommendation:
    """
    5단계 신호 기반 다음 세션 자본 추천.

    단계 (Stages):
        1. halt          — 거래 중단 권고 (multiplier=0)
        2. reduce_strong — 50% 축소
        3. reduce_mild   — 20% 축소
        4. hold          — 유지
        5. increase      — 10% 증액 (긍정 신호 + 무문제 시)

    Returns:
        CapacityRecommendation
    """
    th = thresholds or DEFAULT_THRESHOLDS

    starting = Decimal(str(starting_capital_krw))
    realized = Decimal(str(realized_pnl_krw))
    unrealized = Decimal(str(unrealized_pnl_krw))
    total_pnl = realized + unrealized
    pnl_pct = float(total_pnl / starting) if starting > 0 else 0.0

    signals: list[str] = []
    severity_score = 0  # 0=ok, 1=mild, 2=strong, 3=halt

    # ─── 신호 1: 거부율 ────────────────────────
    if rejection_rate >= th.rejection_rate_critical:
        signals.append(
            f"거부율 {rejection_rate:.1%} (critical, "
            f">={th.rejection_rate_critical:.0%})"
        )
        severity_score = max(severity_score, 2)
    elif rejection_rate >= th.rejection_rate_high:
        signals.append(
            f"거부율 {rejection_rate:.1%} (high, "
            f">={th.rejection_rate_high:.0%})"
        )
        severity_score = max(severity_score, 1)

    # ─── 신호 2: 예외 ──────────────────────────
    if exception_count >= th.exception_count_critical:
        signals.append(
            f"예외 {exception_count}건 (critical, "
            f">={th.exception_count_critical})"
        )
        severity_score = max(severity_score, 2)
    elif exception_count >= th.exception_count_warn:
        signals.append(
            f"예외 {exception_count}건 (warning, "
            f">={th.exception_count_warn})"
        )
        severity_score = max(severity_score, 1)

    # ─── 신호 3: P&L 손실 ──────────────────────
    if pnl_pct <= th.pnl_loss_pct_critical:
        signals.append(
            f"손실 {pnl_pct:.1%} (critical, "
            f"<={th.pnl_loss_pct_critical:.0%})"
        )
        severity_score = max(severity_score, 2)
    elif pnl_pct <= th.pnl_loss_pct_warn:
        signals.append(
            f"손실 {pnl_pct:.1%} (warning, "
            f"<={th.pnl_loss_pct_warn:.0%})"
        )
        severity_score = max(severity_score, 1)

    # ─── 신호 4: 정합성 (Reconciliation) ───────
    if reconciliation_severity == "major":
        signals.append("정합성 불일치 major — 즉시 점검 필요")
        severity_score = max(severity_score, 3)  # halt
    elif reconciliation_severity == "minor":
        signals.append("정합성 불일치 minor")
        severity_score = max(severity_score, 1)

    # ─── 신호 5: 포트폴리오 리스크 경고 ────────
    if portfolio_risk_warnings >= 3:
        signals.append(f"포트폴리오 경고 {portfolio_risk_warnings}건 (high)")
        severity_score = max(severity_score, 2)
    elif portfolio_risk_warnings >= 1:
        signals.append(f"포트폴리오 경고 {portfolio_risk_warnings}건")
        severity_score = max(severity_score, 1)

    # ─── 단계 결정 ─────────────────────────────
    if severity_score >= 3:
        stage = "halt"
        multiplier = th.multiplier_halt
        reasoning = "거래 중단 권고 — 정합성 major 불일치 또는 다중 critical 신호."
    elif severity_score == 2:
        stage = "reduce_strong"
        multiplier = th.multiplier_reduce_strong
        reasoning = "강한 축소 권고 — critical 신호 감지."
    elif severity_score == 1:
        stage = "reduce_mild"
        multiplier = th.multiplier_reduce_mild
        reasoning = "약한 축소 권고 — warning 신호 감지."
    elif total_pnl > 0 and pnl_pct >= 0.02:
        # 신호 없음 + 2% 이상 수익 → 소폭 증액
        stage = "increase"
        multiplier = th.multiplier_increase
        reasoning = "소폭 증액 가능 — 신호 없음 + 안정적 수익."
    else:
        stage = "hold"
        multiplier = th.multiplier_hold
        reasoning = "현재 capacity 유지 권고."

    recommended = (starting * multiplier).quantize(Decimal("1"))

    return CapacityRecommendation(
        current_capital_krw=str(starting),
        recommended_capital_krw=str(recommended),
        multiplier=str(multiplier),
        stage=stage,
        risk_signals=len(signals),
        signal_details=signals,
        reasoning=reasoning,
    )
