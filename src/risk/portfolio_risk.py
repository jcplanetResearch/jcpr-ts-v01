"""
포트폴리오 리스크 분석기 (Portfolio Risk Analyzer)
====================================================

JCPR Trading System - jcpr-ts-v01
Task 47 v0.1

포트폴리오 차원의 리스크 노출/집중도 분석 (read-only).
(Read-only portfolio-level risk exposure/concentration analysis.)

Task 19 게이트는 reject/pass만 결정 — 이 분석기는 종합 스냅샷 + 경고 생성.

원칙:
- Read-only — 부수효과 없음
- Decimal 정밀도
- UTC tz-aware datetime
- 비밀 미포함
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from ..data.symbol_master import SymbolMaster


# ─────────────────────────────────────────────────
# 설정
# ─────────────────────────────────────────────────

@dataclass(frozen=True)
class PortfolioRiskConfig:
    """포트폴리오 리스크 설정 — 한도 모음."""
    max_total_exposure_pct: Decimal = Decimal("0.80")
    max_sector_exposure_pct: Decimal = Decimal("0.40")
    sector_min_diversification: int = 2
    max_single_order_pct_of_equity: Decimal = Decimal("0.10")
    max_correlated_group_exposure_pct: Decimal = Decimal("0.50")
    exempt_etf_from_sector: bool = True

    def __post_init__(self) -> None:
        for name, val in (
            ("max_total_exposure_pct", self.max_total_exposure_pct),
            ("max_sector_exposure_pct", self.max_sector_exposure_pct),
            ("max_single_order_pct_of_equity", self.max_single_order_pct_of_equity),
            ("max_correlated_group_exposure_pct", self.max_correlated_group_exposure_pct),
        ):
            if val <= 0 or val > 1:
                raise ValueError(f"{name}는 (0, 1] 범위: {val}")
        if self.sector_min_diversification < 1:
            raise ValueError(
                f"sector_min_diversification 양수 필요: {self.sector_min_diversification}"
            )


# ─────────────────────────────────────────────────
# 스냅샷 (분석 결과)
# ─────────────────────────────────────────────────

@dataclass(frozen=True)
class PortfolioRiskSnapshot:
    """포트폴리오 리스크 분석 스냅샷."""
    captured_at_utc: datetime
    equity_krw: Decimal

    # 노출
    total_exposure_krw: Decimal
    total_exposure_pct: Decimal              # of equity

    # 섹터별
    by_sector_exposure_krw: dict[str, Decimal] = field(default_factory=dict)
    by_sector_exposure_pct: dict[str, Decimal] = field(default_factory=dict)
    sector_count: int = 0
    max_sector: Optional[str] = None
    max_sector_pct: Decimal = Decimal("0")

    # 종목별
    position_count: int = 0
    etf_count: int = 0
    non_etf_count: int = 0

    # 경고 (한도 초과 등)
    warnings: list[str] = field(default_factory=list)

    def has_warnings(self) -> bool:
        return len(self.warnings) > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "captured_at_utc": self.captured_at_utc.isoformat(),
            "equity_krw": str(self.equity_krw),
            "total_exposure_krw": str(self.total_exposure_krw),
            "total_exposure_pct": f"{self.total_exposure_pct:.6f}",
            "by_sector_exposure_krw": {
                k: str(v) for k, v in self.by_sector_exposure_krw.items()
            },
            "by_sector_exposure_pct": {
                k: f"{v:.6f}" for k, v in self.by_sector_exposure_pct.items()
            },
            "sector_count": self.sector_count,
            "max_sector": self.max_sector,
            "max_sector_pct": f"{self.max_sector_pct:.6f}",
            "position_count": self.position_count,
            "etf_count": self.etf_count,
            "non_etf_count": self.non_etf_count,
            "warnings": list(self.warnings),
        }


# ─────────────────────────────────────────────────
# Analyzer
# ─────────────────────────────────────────────────

class PortfolioRiskAnalyzer:
    """
    포트폴리오 리스크 분석기 (read-only).
    
    Args:
        symbol_master: Task 10 SymbolMaster — sector 조회용
        config: PortfolioRiskConfig — 한도 (None이면 기본값)
    """

    def __init__(
        self,
        symbol_master: SymbolMaster,
        config: Optional[PortfolioRiskConfig] = None,
    ):
        self._sm = symbol_master
        self._config = config or PortfolioRiskConfig()

    @property
    def config(self) -> PortfolioRiskConfig:
        return self._config

    # ------------------------------------------------------------------
    # 분석
    # ------------------------------------------------------------------

    def analyze(
        self,
        *,
        positions: dict[str, dict[str, Any]],
        equity_krw: Decimal,
        as_of_utc: Optional[datetime] = None,
    ) -> PortfolioRiskSnapshot:
        """
        현재 포트폴리오 분석.
        
        Args:
            positions: dict[symbol -> position info] — RiskContext.open_positions 형식
                       각 entry는 'market_value_krw' 키 포함
            equity_krw: 현재 자본
            as_of_utc: 평가 시각 (None이면 now)
        """
        if as_of_utc is None:
            as_of_utc = datetime.now(timezone.utc)
        if as_of_utc.tzinfo is None:
            raise ValueError("as_of_utc tz-aware 필수")
        if equity_krw < 0:
            raise ValueError(f"equity_krw 음수 불가: {equity_krw}")

        return self._build_snapshot(
            positions=positions,
            equity_krw=equity_krw,
            as_of_utc=as_of_utc,
            extra_position=None,
        )

    def project(
        self,
        *,
        positions: dict[str, dict[str, Any]],
        equity_krw: Decimal,
        candidate_symbol: str,
        candidate_side: str,
        candidate_quantity: int,
        candidate_price: Decimal,
        as_of_utc: Optional[datetime] = None,
    ) -> PortfolioRiskSnapshot:
        """
        후보 주문 적용 후 예상 포트폴리오 상태.
        SELL은 단순화 — quantity 차감만 (gross 가격으로 평가, 실제 P&L 변화는 무시)
        BUY는 cost를 추가.
        """
        if as_of_utc is None:
            as_of_utc = datetime.now(timezone.utc)
        if candidate_side not in ("buy", "sell"):
            raise ValueError(f"candidate_side는 'buy' 또는 'sell': {candidate_side!r}")
        if candidate_quantity <= 0:
            raise ValueError(f"candidate_quantity 양수 필요: {candidate_quantity}")
        if candidate_price <= 0:
            raise ValueError(f"candidate_price 양수 필요: {candidate_price}")

        # 가상 positions 만들기
        projected_positions = {k: dict(v) for k, v in positions.items()}
        candidate_cost = candidate_price * Decimal(candidate_quantity)

        if candidate_side == "buy":
            existing = projected_positions.get(candidate_symbol, {})
            existing_value = Decimal(str(existing.get("market_value_krw", "0")))
            projected_positions[candidate_symbol] = {
                **existing,
                "market_value_krw": str(existing_value + candidate_cost),
            }
        else:  # sell
            existing = projected_positions.get(candidate_symbol)
            if existing is None:
                # 보유 없는데 SELL — 분석 단계에서는 무시 (게이트가 reject)
                pass
            else:
                existing_value = Decimal(str(existing.get("market_value_krw", "0")))
                new_value = max(Decimal("0"), existing_value - candidate_cost)
                if new_value <= 0:
                    projected_positions.pop(candidate_symbol, None)
                else:
                    projected_positions[candidate_symbol] = {
                        **existing,
                        "market_value_krw": str(new_value),
                    }

        return self._build_snapshot(
            positions=projected_positions,
            equity_krw=equity_krw,
            as_of_utc=as_of_utc,
            extra_position=None,
        )

    # ------------------------------------------------------------------
    # 내부
    # ------------------------------------------------------------------

    def _build_snapshot(
        self,
        *,
        positions: dict[str, dict[str, Any]],
        equity_krw: Decimal,
        as_of_utc: datetime,
        extra_position: Optional[tuple[str, Decimal]],  # 호환성 placeholder
    ) -> PortfolioRiskSnapshot:
        total_exposure = Decimal("0")
        sector_exposure: dict[str, Decimal] = {}
        etf_count = 0
        non_etf_count = 0

        for sym, pos in positions.items():
            try:
                value = Decimal(str(pos.get("market_value_krw", "0")))
            except Exception:  # noqa: BLE001
                continue
            if value <= 0:
                continue

            total_exposure += value

            sym_obj = self._sm.try_get(sym)
            if sym_obj is None:
                sector = "unknown"
                is_etf = False
            else:
                sector = sym_obj.sector
                is_etf = sym_obj.is_etf()

            if is_etf:
                etf_count += 1
            else:
                non_etf_count += 1

            # ETF는 sector 집계에서 별도 옵션 — config 따라
            if is_etf and self._config.exempt_etf_from_sector:
                # ETF 자체적으로 분산 — 'etf' bucket에 별도 집계 (혹은 무시)
                # v0.1: 'etf' bucket 유지 (가시성)
                sector_exposure["etf"] = sector_exposure.get("etf", Decimal("0")) + value
            else:
                sector_exposure[sector] = sector_exposure.get(sector, Decimal("0")) + value

        # 비율 계산
        if equity_krw > 0:
            total_pct = total_exposure / equity_krw
            sector_pct = {
                s: v / equity_krw for s, v in sector_exposure.items()
            }
        else:
            total_pct = Decimal("0")
            sector_pct = {s: Decimal("0") for s in sector_exposure}

        # 가장 큰 섹터 (ETF 제외 옵션 — 분석은 둘 다 가시화)
        non_etf_sectors = {s: p for s, p in sector_pct.items() if s != "etf"}
        if non_etf_sectors:
            max_sector, max_sector_pct = max(non_etf_sectors.items(), key=lambda x: x[1])
        else:
            max_sector, max_sector_pct = (None, Decimal("0"))

        # 경고 생성
        warnings = self._generate_warnings(
            total_pct=total_pct,
            sector_pct=sector_pct,
            non_etf_sectors=non_etf_sectors,
            equity_krw=equity_krw,
            position_count=len(positions),
        )

        return PortfolioRiskSnapshot(
            captured_at_utc=as_of_utc,
            equity_krw=equity_krw,
            total_exposure_krw=total_exposure,
            total_exposure_pct=total_pct,
            by_sector_exposure_krw=sector_exposure,
            by_sector_exposure_pct=sector_pct,
            sector_count=len([s for s in non_etf_sectors if non_etf_sectors[s] > 0]),
            max_sector=max_sector,
            max_sector_pct=max_sector_pct,
            position_count=len(positions),
            etf_count=etf_count,
            non_etf_count=non_etf_count,
            warnings=warnings,
        )

    def _generate_warnings(
        self,
        *,
        total_pct: Decimal,
        sector_pct: dict[str, Decimal],
        non_etf_sectors: dict[str, Decimal],
        equity_krw: Decimal,
        position_count: int,
    ) -> list[str]:
        """한도 위반 경고 생성."""
        warnings: list[str] = []
        cfg = self._config

        if equity_krw <= 0:
            warnings.append(f"⚠️ 자본 비정상 (equity_krw={equity_krw})")
            return warnings

        # 1) 전체 노출
        if total_pct > cfg.max_total_exposure_pct:
            warnings.append(
                f"⚠️ 전체 노출 한도 초과: "
                f"{total_pct:.2%} > 한도 {cfg.max_total_exposure_pct:.2%}"
            )

        # 2) 섹터 집중도
        for sector, pct in non_etf_sectors.items():
            if pct > cfg.max_sector_exposure_pct:
                warnings.append(
                    f"⚠️ 섹터 집중도 한도 초과: {sector!r} "
                    f"{pct:.2%} > 한도 {cfg.max_sector_exposure_pct:.2%}"
                )

        # 3) 분산 부족
        active_sectors = sum(1 for p in non_etf_sectors.values() if p > 0)
        if position_count > 0 and active_sectors < cfg.sector_min_diversification:
            warnings.append(
                f"⚠️ 섹터 분산 부족: 활성 섹터 {active_sectors}개 "
                f"< 최소 {cfg.sector_min_diversification}개"
            )

        return warnings
