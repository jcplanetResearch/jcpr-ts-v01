"""
포트폴리오 리스크 분석기 (Portfolio Risk Analyzer)
====================================================

JCPR Trading System - jcpr-ts-v01
Task 47 v0.2 — 독립 설계 (Standalone Design)

read-only 분석기. Task 19 게이트에 의존하지 않으며, sector_map은
외부에서 주입 (Task 10 SymbolMaster 또는 별도 dict).
(Read-only analyzer. No Task 19 dependency. sector_map injected externally.)

용도 (Use cases):
    - Task 49 일일 리포트의 portfolio_risk_warnings 입력
    - Task 48 대시보드의 Risk 탭
    - 운영자 즉석 점검 도구

설계 원칙 (Design Principles):
    - 부수효과 없음 (no side effects)
    - 모든 금액 Decimal
    - frozen=True 불변 결과
    - fail-closed: equity_krw=0 → critical (안전 default)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional


# ─────────────────────────────────────────────────
# 상수 (Constants)
# ─────────────────────────────────────────────────

SEVERITY_OK = "ok"
SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"

UNKNOWN_SECTOR = "unknown"
ETF_SECTOR = "etf"


# ─────────────────────────────────────────────────
# 설정 (Config)
# ─────────────────────────────────────────────────

@dataclass(frozen=True)
class PortfolioRiskConfig:
    """포트폴리오 리스크 한도 (Limits)."""
    # 노출 한도 (Exposure limits)
    max_total_exposure_pct: Decimal = Decimal("0.80")
    max_sector_exposure_pct: Decimal = Decimal("0.40")
    max_single_symbol_pct: Decimal = Decimal("0.20")
    max_single_order_pct: Decimal = Decimal("0.10")

    # 분산 (Diversification)
    sector_min_diversification: int = 2

    # ETF 처리 (ETF handling)
    exempt_etf_from_sector: bool = True

    # 경고 임계 (Warning thresholds)
    # critical 임계 = 한도의 X배 (ex: 1.0 = 한도 정확히 도달 시 critical)
    critical_multiplier: Decimal = Decimal("1.0")

    def __post_init__(self) -> None:
        for name, val in (
            ("max_total_exposure_pct", self.max_total_exposure_pct),
            ("max_sector_exposure_pct", self.max_sector_exposure_pct),
            ("max_single_symbol_pct", self.max_single_symbol_pct),
            ("max_single_order_pct", self.max_single_order_pct),
        ):
            if val <= 0 or val > 1:
                raise ValueError(
                    f"{name}는 (0, 1] 범위여야 함: {val}"
                )
        if self.sector_min_diversification < 1:
            raise ValueError(
                f"sector_min_diversification은 양수여야 함: "
                f"{self.sector_min_diversification}"
            )
        if self.critical_multiplier <= 0:
            raise ValueError(
                f"critical_multiplier는 양수여야 함: {self.critical_multiplier}"
            )


# ─────────────────────────────────────────────────
# 스냅샷 (Snapshot)
# ─────────────────────────────────────────────────

@dataclass(frozen=True)
class PortfolioRiskSnapshot:
    """포트폴리오 리스크 분석 결과."""
    # 기본 (Basic)
    equity_krw: Decimal
    total_exposure_krw: Decimal
    total_exposure_pct: Decimal
    cash_krw: Decimal
    cash_pct: Decimal

    # 분포 (Distribution)
    by_symbol: tuple[dict[str, Any], ...]   # 정렬된 종목별
    by_sector: tuple[dict[str, Any], ...]   # 정렬된 섹터별

    # 측정 (Metrics)
    symbol_count: int
    sector_count: int
    hhi: Decimal                            # 0-10000 (Herfindahl)

    # 경고 (Warnings)
    warnings: tuple[str, ...]
    severity: str                           # ok / warning / critical

    # 메타 (Meta)
    computed_at_utc: datetime
    config_used: PortfolioRiskConfig

    def has_warnings(self) -> bool:
        return len(self.warnings) > 0

    def is_ok(self) -> bool:
        return self.severity == SEVERITY_OK

    def to_dict(self) -> dict[str, Any]:
        return {
            "equity_krw": str(self.equity_krw),
            "total_exposure_krw": str(self.total_exposure_krw),
            "total_exposure_pct": str(self.total_exposure_pct),
            "cash_krw": str(self.cash_krw),
            "cash_pct": str(self.cash_pct),
            "symbol_count": self.symbol_count,
            "sector_count": self.sector_count,
            "hhi": str(self.hhi),
            "by_symbol": list(self.by_symbol),
            "by_sector": list(self.by_sector),
            "warnings": list(self.warnings),
            "severity": self.severity,
            "computed_at_utc": self.computed_at_utc.isoformat(),
        }

    def __repr__(self) -> str:
        return (
            f"PortfolioRiskSnapshot(equity={self.equity_krw}, "
            f"exposure={self.total_exposure_pct}, "
            f"symbols={self.symbol_count}, sectors={self.sector_count}, "
            f"severity={self.severity!r}, warnings={len(self.warnings)})"
        )


@dataclass(frozen=True)
class ProjectedImpact:
    """가상 매수/매도 후 영향."""
    before: PortfolioRiskSnapshot
    after: PortfolioRiskSnapshot
    new_warnings: tuple[str, ...]
    would_exceed: dict[str, bool]
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "before": self.before.to_dict(),
            "after": self.after.to_dict(),
            "new_warnings": list(self.new_warnings),
            "would_exceed": self.would_exceed,
            "note": self.note,
        }


# ─────────────────────────────────────────────────
# 분석기 (Analyzer)
# ─────────────────────────────────────────────────

@dataclass(frozen=True)
class PortfolioRiskAnalyzer:
    """
    포트폴리오 리스크 분석기 (read-only).

    sector_map은 {symbol: sector} 형식으로 외부에서 주입.
    누락된 종목은 'unknown' 섹터로 분류.

    사용 (Usage):
        analyzer = PortfolioRiskAnalyzer(
            sector_map={"005930": "tech", "069500": "etf"},
            config=PortfolioRiskConfig(),
        )
        snap = analyzer.analyze(positions=..., equity_krw=...)
        impact = analyzer.project(positions, equity, new_order)
    """

    sector_map: dict[str, str] = field(default_factory=dict)
    config: PortfolioRiskConfig = field(default_factory=PortfolioRiskConfig)

    # ─────────────────────────────────────────
    # 핵심: analyze
    # ─────────────────────────────────────────

    def analyze(
        self,
        *,
        positions: dict[str, dict[str, Any]],
        equity_krw: Decimal,
    ) -> PortfolioRiskSnapshot:
        """
        현재 포지션 종합 분석.

        Args:
            positions: {symbol: {"market_value_krw": Decimal, ...}}
            equity_krw: 총 자본 (현금 + 포지션 시가)

        Returns:
            PortfolioRiskSnapshot
        """
        equity = Decimal(str(equity_krw))

        # ─── 입력 정규화 ───────────────────────
        sym_data: list[dict[str, Any]] = []
        total_exposure = Decimal(0)
        for symbol, info in positions.items():
            mv_raw = info.get("market_value_krw", 0)
            try:
                mv = Decimal(str(mv_raw))
            except Exception:  # noqa: BLE001
                mv = Decimal(0)
            if mv == 0:
                continue
            sector = self._get_sector(symbol)
            sym_data.append({
                "symbol": symbol,
                "market_value_krw": mv,
                "sector": sector,
            })
            total_exposure += mv

        # ─── 노출률 / 현금 ─────────────────────
        if equity <= 0:
            # fail-closed: 자본 0 또는 음수 → 100% 노출 + critical
            total_exposure_pct = Decimal(1) if total_exposure > 0 else Decimal(0)
            cash_pct = Decimal(0)
            cash_krw = Decimal(0) if equity < 0 else (equity - total_exposure)
        else:
            total_exposure_pct = total_exposure / equity
            cash_krw = equity - total_exposure
            cash_pct = cash_krw / equity if equity > 0 else Decimal(0)

        # ─── 종목별 정리 + pct ─────────────────
        for s in sym_data:
            s["pct"] = (s["market_value_krw"] / equity) if equity > 0 else Decimal(0)
        sym_data.sort(key=lambda x: x["market_value_krw"], reverse=True)
        # Decimal → str (직렬화 안전)
        by_symbol = tuple({
            "symbol": s["symbol"],
            "market_value_krw": str(s["market_value_krw"]),
            "pct": str(s["pct"].quantize(Decimal("0.0001"))),
            "sector": s["sector"],
        } for s in sym_data)

        # ─── 섹터별 집계 ───────────────────────
        sector_buckets: dict[str, dict[str, Any]] = {}
        for s in sym_data:
            sec = s["sector"]
            if sec not in sector_buckets:
                sector_buckets[sec] = {
                    "sector": sec,
                    "market_value_krw": Decimal(0),
                    "symbols": [],
                }
            sector_buckets[sec]["market_value_krw"] += s["market_value_krw"]
            sector_buckets[sec]["symbols"].append(s["symbol"])

        for sec, b in sector_buckets.items():
            b["pct"] = (
                b["market_value_krw"] / equity
                if equity > 0 else Decimal(0)
            )

        # 정렬
        sector_list = sorted(
            sector_buckets.values(),
            key=lambda b: b["market_value_krw"],
            reverse=True,
        )
        by_sector = tuple({
            "sector": b["sector"],
            "market_value_krw": str(b["market_value_krw"]),
            "pct": str(b["pct"].quantize(Decimal("0.0001"))),
            "symbols": list(b["symbols"]),
            "symbol_count": len(b["symbols"]),
        } for b in sector_list)

        # ─── HHI (Herfindahl-Hirschman Index) ──
        # 종목별 비율 제곱 합 × 10000 (0-10000 스케일)
        hhi = Decimal(0)
        if equity > 0 and total_exposure > 0:
            for s in sym_data:
                share = s["market_value_krw"] / total_exposure
                hhi += share * share * Decimal(10000)
        hhi = hhi.quantize(Decimal("0.01"))

        # ─── 경고 + severity 평가 ──────────────
        warnings, severity = self._evaluate_warnings(
            equity=equity,
            total_exposure=total_exposure,
            total_exposure_pct=total_exposure_pct,
            sym_data=sym_data,
            sector_buckets=sector_buckets,
        )

        return PortfolioRiskSnapshot(
            equity_krw=equity,
            total_exposure_krw=total_exposure,
            total_exposure_pct=total_exposure_pct.quantize(Decimal("0.0001")),
            cash_krw=cash_krw,
            cash_pct=cash_pct.quantize(Decimal("0.0001")),
            by_symbol=by_symbol,
            by_sector=by_sector,
            symbol_count=len(sym_data),
            sector_count=len(sector_buckets),
            hhi=hhi,
            warnings=tuple(warnings),
            severity=severity,
            computed_at_utc=datetime.now(timezone.utc),
            config_used=self.config,
        )

    # ─────────────────────────────────────────
    # 가상 시나리오: project
    # ─────────────────────────────────────────

    def project(
        self,
        *,
        current_positions: dict[str, dict[str, Any]],
        equity_krw: Decimal,
        new_order: dict[str, Any],
    ) -> ProjectedImpact:
        """
        가상 매수/매도 후 영향 시뮬레이션.

        Args:
            current_positions: 현재 포지션
            equity_krw: 현재 자본
            new_order: {"symbol": str, "side": "buy"|"sell", "value_krw": Decimal}

        Returns:
            ProjectedImpact (before, after, new_warnings, would_exceed)
        """
        before = self.analyze(
            positions=current_positions, equity_krw=equity_krw,
        )

        symbol = new_order.get("symbol")
        side = new_order.get("side", "buy")
        value = Decimal(str(new_order.get("value_krw", 0)))

        if not symbol or value <= 0:
            return ProjectedImpact(
                before=before,
                after=before,
                new_warnings=(),
                would_exceed={},
                note="invalid new_order — symbol/value 누락 또는 0",
            )

        # 가상 포지션 적용
        projected = {k: dict(v) for k, v in current_positions.items()}
        existing_mv = Decimal(str(
            projected.get(symbol, {}).get("market_value_krw", 0)
        ))
        if side == "buy":
            new_mv = existing_mv + value
        elif side == "sell":
            new_mv = max(Decimal(0), existing_mv - value)
        else:
            return ProjectedImpact(
                before=before,
                after=before,
                new_warnings=(),
                would_exceed={},
                note=f"invalid side: {side}",
            )

        if new_mv == 0:
            projected.pop(symbol, None)
        else:
            if symbol not in projected:
                projected[symbol] = {}
            projected[symbol]["market_value_krw"] = new_mv

        after = self.analyze(positions=projected, equity_krw=equity_krw)

        # 새 경고 = after에만 있음
        before_set = set(before.warnings)
        new_warnings = tuple(
            w for w in after.warnings if w not in before_set
        )

        # 한도 초과 여부 (단순 키)
        would_exceed = {
            "total_exposure": after.total_exposure_pct
                > self.config.max_total_exposure_pct,
            "single_symbol": any(
                Decimal(s["pct"]) > self.config.max_single_symbol_pct
                for s in after.by_symbol
            ),
            "single_order": (
                value / Decimal(str(equity_krw))
                if Decimal(str(equity_krw)) > 0 else Decimal(0)
            ) > self.config.max_single_order_pct,
            "sector": any(
                Decimal(b["pct"]) > self.config.max_sector_exposure_pct
                and not (
                    self.config.exempt_etf_from_sector
                    and b["sector"] == ETF_SECTOR
                )
                for b in after.by_sector
            ),
        }

        return ProjectedImpact(
            before=before,
            after=after,
            new_warnings=new_warnings,
            would_exceed=would_exceed,
            note="",
        )

    # ─────────────────────────────────────────
    # 내부 헬퍼 (Private Helpers)
    # ─────────────────────────────────────────

    def _get_sector(self, symbol: str) -> str:
        """sector_map 조회 — 없으면 unknown."""
        return self.sector_map.get(symbol, UNKNOWN_SECTOR)

    def _evaluate_warnings(
        self,
        *,
        equity: Decimal,
        total_exposure: Decimal,
        total_exposure_pct: Decimal,
        sym_data: list[dict[str, Any]],
        sector_buckets: dict[str, dict[str, Any]],
    ) -> tuple[list[str], str]:
        """경고 + severity 평가."""
        warnings: list[str] = []
        max_severity = SEVERITY_OK

        def _bump(s: str) -> None:
            nonlocal max_severity
            order = {SEVERITY_OK: 0, SEVERITY_WARNING: 1, SEVERITY_CRITICAL: 2}
            if order.get(s, 0) > order.get(max_severity, 0):
                max_severity = s

        # ─── 0. equity 검증 ────────────────────
        if equity <= 0 and total_exposure > 0:
            warnings.append(
                f"❗ 자본 ≤ 0 (equity={equity}) 인데 노출 있음 — "
                f"즉시 운영자 점검 필요"
            )
            _bump(SEVERITY_CRITICAL)
            return warnings, max_severity

        # ─── 1. 전체 노출 ──────────────────────
        if total_exposure_pct > self.config.max_total_exposure_pct:
            level = (
                SEVERITY_CRITICAL
                if total_exposure_pct >= self.config.max_total_exposure_pct
                    * self.config.critical_multiplier
                else SEVERITY_WARNING
            )
            warnings.append(
                f"전체 노출 {total_exposure_pct:.1%} > 한도 "
                f"{self.config.max_total_exposure_pct:.1%}"
            )
            _bump(level)

        # ─── 2. 단일 종목 ──────────────────────
        for s in sym_data:
            if s["pct"] > self.config.max_single_symbol_pct:
                warnings.append(
                    f"종목 {s['symbol']} 비율 {s['pct']:.1%} > 한도 "
                    f"{self.config.max_single_symbol_pct:.1%}"
                )
                _bump(SEVERITY_WARNING)

        # ─── 3. 섹터 집중도 ────────────────────
        for sec, b in sector_buckets.items():
            # ETF 면제
            if self.config.exempt_etf_from_sector and sec == ETF_SECTOR:
                continue
            if b["pct"] > self.config.max_sector_exposure_pct:
                warnings.append(
                    f"섹터 {sec} 비율 {b['pct']:.1%} > 한도 "
                    f"{self.config.max_sector_exposure_pct:.1%} "
                    f"(종목: {', '.join(b['symbols'][:3])}"
                    f"{'...' if len(b['symbols']) > 3 else ''})"
                )
                _bump(SEVERITY_WARNING)

        # ─── 4. 분산 부족 ──────────────────────
        non_etf_sectors = sum(
            1 for sec in sector_buckets
            if not (
                self.config.exempt_etf_from_sector and sec == ETF_SECTOR
            )
        )
        if (len(sym_data) > 0
                and non_etf_sectors < self.config.sector_min_diversification):
            warnings.append(
                f"섹터 분산 부족 — 비-ETF 섹터 {non_etf_sectors}개 < "
                f"최소 {self.config.sector_min_diversification}개"
            )
            _bump(SEVERITY_WARNING)

        # ─── 5. unknown 섹터 다량 ──────────────
        if UNKNOWN_SECTOR in sector_buckets:
            unk_pct = sector_buckets[UNKNOWN_SECTOR]["pct"]
            if unk_pct > Decimal("0.5"):
                warnings.append(
                    f"섹터 미분류 비율 {unk_pct:.1%} > 50% — "
                    f"sector_map 보강 권장"
                )
                _bump(SEVERITY_WARNING)

        return warnings, max_severity


# ─────────────────────────────────────────────────
# 편의 함수 (Convenience)
# ─────────────────────────────────────────────────

def quick_analyze(
    positions: dict[str, dict[str, Any]],
    equity_krw: Decimal,
    *,
    sector_map: Optional[dict[str, str]] = None,
    config: Optional[PortfolioRiskConfig] = None,
) -> PortfolioRiskSnapshot:
    """One-shot 분석 — analyzer 객체 생성 없이."""
    analyzer = PortfolioRiskAnalyzer(
        sector_map=sector_map or {},
        config=config or PortfolioRiskConfig(),
    )
    return analyzer.analyze(positions=positions, equity_krw=equity_krw)
