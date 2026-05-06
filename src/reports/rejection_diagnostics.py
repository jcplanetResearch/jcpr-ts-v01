"""
거부 패턴 진단 룰 (Rejection Pattern Diagnostics)
==================================================

JCPR Trading System - jcpr-ts-v01
Task 20 v0.1

거부 분석 결과를 룰로 검사하여 운영자 권장 조치 생성.
(Inspect rejection analysis with rules to produce operator recommendations.)

원칙:
- 룰은 순수 함수 (no side effects)
- 임계값은 호출자가 override 가능
- 데이터 부족 시 진단 보류 (false negative 우선)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# 기본 임계값
DEFAULT_THRESHOLDS = {
    "rate_limit_concern_pct": 0.30,       # rate_limit 거부율 30% 이상
    "exposure_concern_pct": 0.30,
    "single_symbol_dominance_pct": 0.70,  # 단일 종목이 거부의 70% 이상 차지
    "rolling_rate_concern": 0.50,         # 30분 윈도우 거부율 50% 이상
    "min_total_for_analysis": 10,         # 최소 평가 수
}


SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"


@dataclass(frozen=True)
class DiagnosticFinding:
    """진단 결과 1건."""
    severity: str                                  # "info" / "warning" / "critical"
    code: str                                      # 식별 코드 (예: "high_rate_limit_rejections")
    message: str                                   # 사람용 메시지 (Korean)
    recommendation: str                            # 권장 조치
    related_gate: Optional[str] = None
    related_symbol: Optional[str] = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "recommendation": self.recommendation,
            "related_gate": self.related_gate,
            "related_symbol": self.related_symbol,
            "detail": dict(self.detail),
        }


# ─────────────────────────────────────────────────
# 진단 룰
# ─────────────────────────────────────────────────

def diagnose(
    *,
    total_evaluations: int,
    reject_count: int,
    by_gate_reject: dict[str, int],
    by_symbol_reject: dict[str, int],
    rolling_rates: list[dict],
    thresholds: Optional[dict[str, float]] = None,
) -> list[DiagnosticFinding]:
    """
    거부 분석 결과 → 진단 결과 리스트.

    Args:
        total_evaluations: 전체 평가 수 (pass + reject)
        reject_count: 총 거부 수
        by_gate_reject: 게이트별 거부 횟수
        by_symbol_reject: 종목별 거부 횟수
        rolling_rates: [{window_start_utc, rate, count, ...}]
        thresholds: override 임계값
    """
    th = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    findings: list[DiagnosticFinding] = []

    # 데이터 부족
    if total_evaluations < th["min_total_for_analysis"]:
        findings.append(DiagnosticFinding(
            severity=SEVERITY_INFO,
            code="insufficient_data",
            message=(
                f"데이터 부족 — 평가 {total_evaluations}건 < "
                f"최소 {int(th['min_total_for_analysis'])}건"
            ),
            recommendation=(
                "더 많은 거래 활동 후 재분석 권장 "
                "(insufficient evaluations for reliable analysis)"
            ),
            detail={"total_evaluations": total_evaluations},
        ))
        return findings

    # ── 1) Kill switch — 1회라도 발생하면 critical ──
    kill_count = by_gate_reject.get("kill_switch", 0)
    if kill_count > 0:
        findings.append(DiagnosticFinding(
            severity=SEVERITY_CRITICAL,
            code="kill_switch_activated",
            message=f"⛔ Kill switch 활성화 감지 — {kill_count}건 거부",
            recommendation=(
                "KILL_SWITCH_FILE 경로 점검 — 의도된 정지인지 확인. "
                "의도된 정지가 아니면 파일 삭제 후 재시작."
            ),
            related_gate="kill_switch",
            detail={"reject_count": kill_count},
        ))

    # ── 2) Daily loss — 1회라도 발생 ──
    loss_count = by_gate_reject.get("daily_loss_limit", 0)
    if loss_count > 0:
        findings.append(DiagnosticFinding(
            severity=SEVERITY_CRITICAL,
            code="daily_loss_limit_hit",
            message=f"🔴 일일 손실 한도 도달 — {loss_count}건 거부",
            recommendation=(
                "운영자 검토 필수 — 손실 한도가 의도한 수준인지 확인. "
                "전략 신호 품질 + 리스크 한도 재검토 후 다음 세션 결정."
            ),
            related_gate="daily_loss_limit",
            detail={"reject_count": loss_count},
        ))

    # ── 3) Rate limit — 비율 검사 ──
    rate_count = by_gate_reject.get("order_rate_limit", 0)
    if reject_count > 0:
        rate_pct = rate_count / reject_count
        if rate_pct >= th["rate_limit_concern_pct"]:
            findings.append(DiagnosticFinding(
                severity=SEVERITY_WARNING,
                code="high_rate_limit_rejections",
                message=(
                    f"주문 빈도 제한 거부 다수 — "
                    f"전체 거부의 {rate_pct:.1%} ({rate_count}/{reject_count})"
                ),
                recommendation=(
                    "신호 생성 빈도 검토 — RATE_MIN_INTERVAL_SEC 증가 또는 "
                    "신호 필터 강화 (낮은 confidence 임계값 인상)."
                ),
                related_gate="order_rate_limit",
                detail={
                    "reject_count": rate_count,
                    "share_of_rejects": round(rate_pct, 4),
                },
            ))

    # ── 4) Exposure (per-symbol or portfolio) — 비율 검사 ──
    exposure_count = (
        by_gate_reject.get("exposure_per_symbol", 0)
        + by_gate_reject.get("portfolio_total_exposure", 0)
    )
    if reject_count > 0 and exposure_count > 0:
        exp_pct = exposure_count / reject_count
        if exp_pct >= th["exposure_concern_pct"]:
            findings.append(DiagnosticFinding(
                severity=SEVERITY_WARNING,
                code="high_exposure_rejections",
                message=(
                    f"노출 한도 거부 다수 — "
                    f"전체 거부의 {exp_pct:.1%} ({exposure_count}/{reject_count})"
                ),
                recommendation=(
                    "포트폴리오 한도 재검토 — 종목별/전체 노출 한도 재설정 또는 "
                    "기존 포지션 축소 후 재진입."
                ),
                detail={
                    "reject_count": exposure_count,
                    "share_of_rejects": round(exp_pct, 4),
                },
            ))

    # ── 5) Sector concentration ──
    sector_count = by_gate_reject.get("sector_concentration", 0)
    if sector_count > 0 and reject_count > 0:
        sec_pct = sector_count / reject_count
        if sec_pct >= th["exposure_concern_pct"]:
            findings.append(DiagnosticFinding(
                severity=SEVERITY_WARNING,
                code="high_sector_concentration_rejections",
                message=(
                    f"섹터 집중도 거부 다수 — "
                    f"{sec_pct:.1%} ({sector_count}/{reject_count})"
                ),
                recommendation=(
                    "포트폴리오가 특정 섹터에 집중됨. "
                    "다른 섹터의 종목으로 분산 또는 max_sector_exposure_pct 재검토."
                ),
                related_gate="sector_concentration",
                detail={
                    "reject_count": sector_count,
                    "share_of_rejects": round(sec_pct, 4),
                },
            ))

    # ── 6) Market state — 다수 거부 ──
    market_count = by_gate_reject.get("market_state", 0)
    if market_count >= 5:  # 단순 임계값 (시장 외 신호는 5건 이상이면 패턴)
        findings.append(DiagnosticFinding(
            severity=SEVERITY_WARNING,
            code="market_state_rejections",
            message=f"시장 외 신호 다수 — {market_count}건 거부",
            recommendation=(
                "신호 생성 스케줄 검토 — 시장 시간(09:00-15:30 KST) 외에는 "
                "신호 생성을 일시 중단하도록 SignalRunner.market_is_open_provider 설정."
            ),
            related_gate="market_state",
            detail={"reject_count": market_count},
        ))

    # ── 7) Price sanity — 가격 데이터 신선도 ──
    price_count = by_gate_reject.get("price_sanity", 0)
    if price_count >= 3:
        findings.append(DiagnosticFinding(
            severity=SEVERITY_WARNING,
            code="price_sanity_rejections",
            message=f"가격 합리성 거부 — {price_count}건",
            recommendation=(
                "가격 데이터 신선도 점검 — Quote 데이터 ingestion 빈도 확인 "
                "(Task 13). max_quote_age_sec 임계값과 실제 갱신 주기 일치 여부."
            ),
            related_gate="price_sanity",
            detail={"reject_count": price_count},
        ))

    # ── 8) 단일 종목 지배 ──
    if reject_count >= 5 and by_symbol_reject:
        top_sym, top_count = max(by_symbol_reject.items(), key=lambda x: x[1])
        sym_pct = top_count / reject_count
        if sym_pct >= th["single_symbol_dominance_pct"]:
            findings.append(DiagnosticFinding(
                severity=SEVERITY_WARNING,
                code="single_symbol_dominance",
                message=(
                    f"단일 종목 거부 집중 — `{top_sym}`: "
                    f"{sym_pct:.1%} ({top_count}/{reject_count})"
                ),
                recommendation=(
                    f"종목 `{top_sym}` 점검: "
                    "watchlist 유지 필요성, 데이터 품질, 기존 포지션 상태."
                ),
                related_symbol=top_sym,
                detail={
                    "symbol": top_sym,
                    "reject_count": top_count,
                    "share_of_rejects": round(sym_pct, 4),
                },
            ))

    # ── 9) 시간대 윈도우 — 거부율 폭증 ──
    if rolling_rates:
        for w in rolling_rates:
            count = w.get("count", 0)
            rate = w.get("rate", 0.0)
            if count >= th["min_total_for_analysis"] and rate >= th["rolling_rate_concern"]:
                findings.append(DiagnosticFinding(
                    severity=SEVERITY_WARNING,
                    code="window_rejection_spike",
                    message=(
                        f"시간대 거부율 폭증 — "
                        f"{w.get('window_start_kst', '?')}: "
                        f"{rate:.1%} ({w.get('reject_count', 0)}/{count})"
                    ),
                    recommendation=(
                        "해당 시간대 시장 상황 점검 — 일시적 변동성 또는 "
                        "데이터 이상 가능성. 거래 일시 중단 검토."
                    ),
                    detail={
                        "window_start_utc": w.get("window_start_utc"),
                        "rate": round(rate, 4),
                        "reject_count": w.get("reject_count"),
                        "total": count,
                    },
                ))

    # ── 10) 정상 (no findings) ──
    if not findings:
        findings.append(DiagnosticFinding(
            severity=SEVERITY_INFO,
            code="no_significant_patterns",
            message="✅ 특이 패턴 없음 — 거부 분포 정상 범위",
            recommendation="현재 설정 유지. 정기 모니터링 지속.",
        ))

    return findings
