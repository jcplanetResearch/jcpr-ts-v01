"""
모멘텀 전략 v0.4 (Momentum Strategy v0.4)
==========================================

JCPR Trading System - jcpr-ts-v01
Task 14 v0.4

다중 신호 합성 (Multi-factor Confluence):
- Price Momentum   (Task 12 OHLCV close 시리즈)
- Volume Confirm   (Task 12 volume 시리즈)
- Buy-Sell Intensity (Task 12 up/down volume — Tick classification)
- CVD Trend        (Task 12 cumulative volume delta)
- Quote Imbalance  (Task 13 best/depth bid-ask imbalance)
- Spread Quality   (Task 13 spread_bps)

원칙 (Principles):
- fail-closed: 데이터 부족/stale 시 해당 지표 None → composite에서 제외
- 데이터 신뢰도 가중: volume_split_method가 estimated_*면 강도 신호 신뢰도 ↓
- 호가 stale 시 quote 신호 자동 무시
- 모든 시각 UTC tz-aware
- Symbol Master 통합 — 거래정지 종목 거부

이전 버전 (v0.3) 대비 변경 (Changes from v0.3):
- 단일 가격 모멘텀 → 6개 지표 합성
- Symbol Master 통합 (거래정지 종목 fail-closed)
- 호가 데이터 통합 (stale 시 자동 폴백)
- Composite score + confidence 분리 (호출자가 confidence 기반 필터 가능)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from ...data.ohlcv_schema import Timeframe, VolumeSplitMethod
from ..schema_v2 import MomentumSignalV04, SignalSide
from .indicators import (
    compute_price_momentum,
    compute_volume_confirmation,
    compute_buy_sell_intensity,
    compute_cvd_trend,
    compute_quote_imbalance,
    compute_spread_quality,
)

logger = logging.getLogger(__name__)


# 데이터 신뢰도 가중치 (split_method → weight multiplier)
SPLIT_METHOD_RELIABILITY = {
    VolumeSplitMethod.SOURCE_PROVIDED: Decimal("1.00"),
    VolumeSplitMethod.ESTIMATED_HYBRID: Decimal("0.70"),
    VolumeSplitMethod.ESTIMATED_SIMPLE: Decimal("0.50"),
    VolumeSplitMethod.ESTIMATED_INTRABAR: Decimal("0.40"),
    VolumeSplitMethod.UNKNOWN: Decimal("0.00"),
}


@dataclass(frozen=True)
class MomentumV04Config:
    """모멘텀 v0.4 설정."""
    # Lookback periods
    price_lookback: int = 5
    volume_short_window: int = 3
    volume_long_window: int = 10
    intensity_lookback: int = 5
    cvd_lookback: int = 5

    # 가중치 (sum should be 1.0)
    weight_price: Decimal = Decimal("0.35")
    weight_volume: Decimal = Decimal("0.15")
    weight_intensity: Decimal = Decimal("0.20")
    weight_cvd: Decimal = Decimal("0.15")
    weight_quote_imb: Decimal = Decimal("0.10")
    weight_spread_quality: Decimal = Decimal("0.05")

    # 임계값
    threshold_buy: Decimal = Decimal("0.20")
    threshold_sell: Decimal = Decimal("-0.20")
    min_confidence: Decimal = Decimal("0.50")

    # 호가 신선도
    max_quote_age_sec: int = 30

    # CVD 정규화
    cvd_normalization: int = 100_000


class MomentumStrategyV04:
    """
    Task 14 v0.4 모멘텀 전략.
    
    호출 시 종목/시각 입력 → MomentumSignalV04 출력.
    """

    strategy_id = "momentum_v04"

    def __init__(
        self,
        ohlcv_store,                   # OHLCVStore (Task 12)
        quote_store=None,              # QuoteStore (Task 13) — 옵션
        symbol_master=None,            # SymbolMaster (Task 10) — 옵션
        config: Optional[MomentumV04Config] = None,
    ):
        self._ohlcv = ohlcv_store
        self._quote = quote_store
        self._sm = symbol_master
        self._cfg = config or MomentumV04Config()
        # 가중치 합 검증
        total_w = (
            self._cfg.weight_price + self._cfg.weight_volume
            + self._cfg.weight_intensity + self._cfg.weight_cvd
            + self._cfg.weight_quote_imb + self._cfg.weight_spread_quality
        )
        if not (Decimal("0.99") <= total_w <= Decimal("1.01")):
            raise ValueError(f"가중치 합이 1.0 근처여야 함: {total_w}")

    def generate(
        self,
        symbol: str,
        timeframe: Timeframe,
        as_of_utc: datetime,
    ) -> MomentumSignalV04:
        """
        시그널 생성.
        
        Args:
            symbol: KRX 코드
            timeframe: 봉 단위
            as_of_utc: 시그널 시점 (이 시각 이전 데이터만 사용)
        
        Returns:
            MomentumSignalV04 — flat이면 미행동, 그 외는 buy/sell.
        """
        if as_of_utc.tzinfo is None:
            raise ValueError("as_of_utc tz-aware 필수")

        # Symbol Master 검증 (있으면)
        if self._sm is not None and not self._sm.is_tradable(symbol):
            return self._flat_signal(
                symbol, as_of_utc,
                reason="symbol not tradable",
            )

        # OHLCV 데이터 조회 — 최대 lookback 기준
        max_lookback = max(
            self._cfg.price_lookback + 1,
            self._cfg.volume_long_window,
            self._cfg.intensity_lookback,
            self._cfg.cvd_lookback + 1,
        ) + 5  # 여유분
        # timeframe 기간을 단순화: 일봉 기준 max_lookback일, 분봉은 같은 봉 수
        bars = self._fetch_recent_bars(symbol, timeframe, as_of_utc, max_lookback)
        if not bars:
            return self._flat_signal(
                symbol, as_of_utc,
                reason="no OHLCV data",
            )

        # 컴포넌트 계산
        components: dict[str, Optional[Decimal]] = {}
        reliability: dict[str, Decimal] = {}
        metadata: dict = {
            "n_bars_used": len(bars),
            "as_of_utc": as_of_utc.isoformat(),
        }

        # 1) Price Momentum
        closes = [b.close for b in bars]
        components["price"] = compute_price_momentum(closes, self._cfg.price_lookback)
        reliability["price"] = Decimal("1.00")  # OHLC는 항상 신뢰

        # 2) Volume Confirmation — direction은 price 부호 기반
        volumes = [b.volume for b in bars]
        direction = 1 if (components["price"] or Decimal("0")) >= 0 else -1
        components["volume"] = compute_volume_confirmation(
            volumes, self._cfg.volume_short_window, self._cfg.volume_long_window,
            direction=direction,
        )
        reliability["volume"] = Decimal("1.00")

        # 3) Buy-Sell Intensity (Task 12 분류)
        intensities = [b.buy_sell_intensity() for b in bars]
        components["intensity"] = compute_buy_sell_intensity(
            intensities, self._cfg.intensity_lookback,
        )
        # 신뢰도 = 최근 봉들의 split_method 평균 신뢰도
        recent_methods = [b.volume_split_method for b in bars[-self._cfg.intensity_lookback:]]
        reliability["intensity"] = self._avg_reliability(recent_methods)

        # 4) CVD Trend
        cvd_series = self._build_cvd_series(bars)
        components["cvd"] = compute_cvd_trend(
            cvd_series, self._cfg.cvd_lookback,
            normalization_threshold=self._cfg.cvd_normalization,
        )
        reliability["cvd"] = reliability["intensity"]  # CVD도 분류에 의존

        # 5/6) Quote 신호 — quote_store 있고 신선할 때만
        quote_imb: Optional[Decimal] = None
        spread_q: Optional[Decimal] = None
        if self._quote is not None:
            try:
                snap = self._quote.latest_fresh(
                    symbol, as_of_utc, self._cfg.max_quote_age_sec,
                )
            except Exception as e:  # noqa: BLE001
                snap = None
                metadata["quote_error"] = f"{type(e).__name__}"

            if snap is not None:
                quote_imb = compute_quote_imbalance(snap)
                spread_q = compute_spread_quality(snap)
                metadata["quote_age_sec"] = f"{snap.age_seconds(as_of_utc):.2f}"
                metadata["quote_source"] = snap.source
                reliability["quote_imb"] = Decimal("1.00") if snap.is_live_source else Decimal("0.50")
                reliability["spread_quality"] = reliability["quote_imb"]
            else:
                metadata["quote_status"] = "stale_or_missing"
                reliability["quote_imb"] = Decimal("0.00")
                reliability["spread_quality"] = Decimal("0.00")
        else:
            reliability["quote_imb"] = Decimal("0.00")
            reliability["spread_quality"] = Decimal("0.00")

        components["quote_imb"] = quote_imb
        components["spread_quality"] = spread_q

        # ------ 합성 (Composite) ------
        # 각 지표: 신뢰도 가중치 적용 후 가중 합산
        weights = {
            "price": self._cfg.weight_price,
            "volume": self._cfg.weight_volume,
            "intensity": self._cfg.weight_intensity,
            "cvd": self._cfg.weight_cvd,
            "quote_imb": self._cfg.weight_quote_imb,
            "spread_quality": self._cfg.weight_spread_quality,
        }

        weighted_sum = Decimal("0")
        effective_weight_sum = Decimal("0")
        valid_components: dict[str, Decimal] = {}

        for key, val in components.items():
            if val is None:
                continue
            rel = reliability.get(key, Decimal("0"))
            if rel <= 0:
                continue
            w_eff = weights[key] * rel
            # spread_quality는 방향성 없음 → 곱하기보다 confidence 가중에 사용
            # 단순화를 위해 v0.4에서는 모든 지표를 동일 방식으로 합산
            weighted_sum += val * w_eff
            effective_weight_sum += w_eff
            valid_components[key] = val

        if effective_weight_sum == 0:
            return self._flat_signal(
                symbol, as_of_utc,
                reason="no valid indicators",
                metadata=metadata,
                components_so_far={k: v for k, v in components.items() if v is not None},
            )

        # 정규화 (effective_weight_sum이 1보다 작으면 가중치 재정규화)
        composite = weighted_sum / effective_weight_sum
        # 클램핑
        if composite > Decimal("1"):
            composite = Decimal("1")
        elif composite < Decimal("-1"):
            composite = Decimal("-1")

        # ------ Confidence (signal confluence) ------
        # 부호 일치도 — 같은 방향을 가리킨 지표 비율
        signs = [
            1 if v > 0 else (-1 if v < 0 else 0)
            for k, v in valid_components.items()
            if k != "spread_quality"  # spread_quality는 항상 양수
        ]
        if not signs:
            confidence = Decimal("0")
        else:
            net_sign = sum(signs)
            confidence = Decimal(abs(net_sign)) / Decimal(len(signs))

        # ------ Side 결정 ------
        if composite >= self._cfg.threshold_buy:
            side = SignalSide.BUY
        elif composite <= self._cfg.threshold_sell:
            side = SignalSide.SELL
        else:
            side = SignalSide.FLAT

        # confidence 부족 시 FLAT으로 강제
        if side != SignalSide.FLAT and confidence < self._cfg.min_confidence:
            metadata["downgraded_to_flat"] = (
                f"confidence({confidence:.4f}) < min({self._cfg.min_confidence})"
            )
            side = SignalSide.FLAT

        metadata["effective_weight_sum"] = str(effective_weight_sum)
        metadata["reliability"] = {k: str(v) for k, v in reliability.items()}

        return MomentumSignalV04(
            symbol=symbol,
            timestamp_utc=as_of_utc,
            composite_score=composite,
            side=side,
            confidence=confidence,
            components={k: v for k, v in valid_components.items()},
            metadata=metadata,
            strategy_id=self.strategy_id,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _fetch_recent_bars(
        self, symbol: str, timeframe: Timeframe,
        as_of_utc: datetime, n_bars: int,
    ) -> list:
        """as_of_utc 이전 N개 봉."""
        # 시간 범위 추정 (timeframe 기준 넉넉히)
        timeframe_seconds = {
            Timeframe.M1: 60, Timeframe.M5: 300, Timeframe.M15: 900,
            Timeframe.M60: 3600, Timeframe.D1: 86400,
        }
        delta_sec = timeframe_seconds[timeframe]
        # 일봉의 경우 주말/휴장 고려해 2배 여유
        multiplier = 3 if timeframe == Timeframe.D1 else 2
        start = as_of_utc - timedelta(seconds=delta_sec * n_bars * multiplier)
        bars = self._ohlcv.fetch(symbol, timeframe, start, as_of_utc)
        return bars[-n_bars:] if bars else []

    @staticmethod
    def _build_cvd_series(bars: list) -> list[int]:
        """봉 시리즈에서 CVD 누적 시리즈 생성."""
        cvd = 0
        series = []
        for b in bars:
            if b.up_volume is not None and b.down_volume is not None:
                cvd += (b.up_volume - b.down_volume)
            series.append(cvd)
        return series

    @staticmethod
    def _avg_reliability(methods: list) -> Decimal:
        """split_method 리스트의 평균 신뢰도."""
        if not methods:
            return Decimal("0")
        weights = [SPLIT_METHOD_RELIABILITY.get(m, Decimal("0")) for m in methods]
        return sum(weights) / Decimal(len(weights))

    def _flat_signal(
        self, symbol: str, as_of_utc: datetime,
        *, reason: str = "",
        metadata: Optional[dict] = None,
        components_so_far: Optional[dict] = None,
    ) -> MomentumSignalV04:
        meta = metadata or {}
        meta["flat_reason"] = reason
        return MomentumSignalV04(
            symbol=symbol,
            timestamp_utc=as_of_utc,
            composite_score=Decimal("0"),
            side=SignalSide.FLAT,
            confidence=Decimal("0"),
            components=components_so_far or {},
            metadata=meta,
            strategy_id=self.strategy_id,
        )
