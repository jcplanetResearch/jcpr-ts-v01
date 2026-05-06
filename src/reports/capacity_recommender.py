"""
다음 세션 자본 추천 (Next-Session Capacity Recommender)
========================================================

JCPR Trading System - jcpr-ts-v01
Task 49 v0.1 — Final Output #12

5단계 시그널 강도 기반 보수적 추천.
(5-stage signal-strength based conservative recommendation.)

원칙:
- 보수적 (conservative) — 위험 신호 시 capacity 축소
- 운영자 결정의 참고 자료일 뿐 — 강제 아님
- 모든 입력은 read-only (audit log + 분석 결과)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional


@dataclass(frozen=True)
class CapacityRecommendation:
    """다음 세션 자본 추천 결과 — Final Output #12."""
    current_capital_krw: Decimal
    recommend_pct: Decimal              # 0.50 ~ 1.00
    recommended_capacity_krw: Decimal
    risk_signals: int
    risk_signal_breakdown: dict[str, int] = field(default_factory=dict)
    reasoning: list[str] = field(default_factory=list)
    severity: str = "ok"                # "ok" / "low" / "moderate" / "high" / "critical"

    def to_dict(self) -> dict:
        return {
            "current_capital_krw": str(self.current_capital_krw),
            "recommend_pct": str(self.recommend_pct),
            "recommended_capacity_krw": str(self.recommended_capacity_krw),
            "risk_signals": self.risk_signals,
            "risk_signal_breakdown": dict(self.risk_signal_breakdown),
            "severity": self.severity,
            "reasoning": list(self.reasoning),
        }


def recommend_next_capacity(
    *,
    starting_capital_krw: Decimal,
    realized_pnl_krw: Decimal,
    rejected_orders_count: int,
    rejection_rate: float,
    exceptions_count: int,
    reconciliation_severity: str = "ok",   # "ok" / "minor" / "major" / "unknown"
    portfolio_risk_warnings: int = 0,
    rejection_threshold_count: int = 50,
    rejection_threshold_rate: float = 0.30,
) -> CapacityRecommendation:
    """
    5단계 자본 추천.

    Signal Sources (each contributes weighted points):
        1. Reconciliation severity:
           - major: +2 (severe — broker/internal mismatch)
           - minor: +1 (avg_price drift only)
           - unknown: +1 (data missing — conservative)
           - ok: 0

        2. Exception count: > 0 → +1

        3. Rejection volume:
           - rejected_count > threshold (default 50): +1
           - rejection_rate > threshold (default 30%): +1 (independent)

        4. Portfolio risk warnings: > 0 → +1

    Stage mapping (signal_total → capacity_pct):
        0 signals: 100% (정상 — maintain)
        1 signal:  90%  (주의 — slight reduction)
        2 signals: 75%  (경고 — moderate reduction)
        3 signals: 60%  (위험 — substantial reduction)
        4+ signals: 50% (심각 — minimum capacity / forced review)
    """
    if starting_capital_krw < 0:
        raise ValueError(f"starting_capital_krw 음수 불가: {starting_capital_krw}")
    if rejected_orders_count < 0 or exceptions_count < 0 or portfolio_risk_warnings < 0:
        raise ValueError("count 인자는 음수 불가")
    if not (0.0 <= rejection_rate <= 1.0):
        raise ValueError(f"rejection_rate는 [0, 1]: {rejection_rate}")

    # 누적 자본 = 시작 + 실현 P&L (미실현은 제외 — 보수적)
    current_capital = starting_capital_krw + realized_pnl_krw
    if current_capital < 0:
        current_capital = Decimal("0")

    # 신호 누적
    signals = 0
    breakdown: dict[str, int] = {}
    reasoning: list[str] = []

    # 1) Reconciliation
    if reconciliation_severity == "major":
        signals += 2
        breakdown["reconciliation_major"] = 2
        reasoning.append(
            "정합성(reconciliation) major mismatch 감지 — "
            "브로커-내부 원장 불일치 (broker-ledger discrepancy)"
        )
    elif reconciliation_severity == "minor":
        signals += 1
        breakdown["reconciliation_minor"] = 1
        reasoning.append(
            "정합성 minor mismatch — 평균가 차이 (avg_price drift)"
        )
    elif reconciliation_severity == "unknown":
        signals += 1
        breakdown["reconciliation_unknown"] = 1
        reasoning.append(
            "정합성 점검 미실행 — 데이터 부재 (reconciliation not performed)"
        )

    # 2) 예외
    if exceptions_count > 0:
        signals += 1
        breakdown["exceptions"] = 1
        reasoning.append(
            f"실행 중 예외 {exceptions_count}건 발생 (exceptions during execution)"
        )

    # 3) 거부 폭증 — 횟수
    if rejected_orders_count > rejection_threshold_count:
        signals += 1
        breakdown["rejection_volume"] = 1
        reasoning.append(
            f"거부된 주문 {rejected_orders_count}건 > 임계값 {rejection_threshold_count} "
            f"(high rejection volume)"
        )

    # 3-2) 거부율
    if rejection_rate > rejection_threshold_rate:
        signals += 1
        breakdown["rejection_rate"] = 1
        reasoning.append(
            f"거부율 {rejection_rate:.1%} > 임계값 {rejection_threshold_rate:.1%} "
            f"(high rejection rate)"
        )

    # 4) 포트폴리오 위험 경고
    if portfolio_risk_warnings > 0:
        signals += 1
        breakdown["portfolio_warnings"] = 1
        reasoning.append(
            f"포트폴리오 리스크 경고 {portfolio_risk_warnings}건 "
            f"(portfolio risk warnings)"
        )

    # 5단계 매핑
    if signals == 0:
        pct = Decimal("1.00")
        severity = "ok"
        reasoning.insert(0, "정상 — 위험 신호 없음 (no risk signals)")
    elif signals == 1:
        pct = Decimal("0.90")
        severity = "low"
        reasoning.insert(0, "주의 — 1개 위험 신호, 90% 권장")
    elif signals == 2:
        pct = Decimal("0.75")
        severity = "moderate"
        reasoning.insert(0, "경고 — 2개 위험 신호, 75% 권장")
    elif signals == 3:
        pct = Decimal("0.60")
        severity = "high"
        reasoning.insert(0, "위험 — 3개 위험 신호, 60% 권장")
    else:  # 4+
        pct = Decimal("0.50")
        severity = "critical"
        reasoning.insert(
            0,
            f"심각 — {signals}개 위험 신호, 50% 권장 (운영자 검토 필수)"
        )

    recommended = current_capital * pct

    return CapacityRecommendation(
        current_capital_krw=current_capital,
        recommend_pct=pct,
        recommended_capacity_krw=recommended,
        risk_signals=signals,
        risk_signal_breakdown=breakdown,
        reasoning=reasoning,
        severity=severity,
    )
