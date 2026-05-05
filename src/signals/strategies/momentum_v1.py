"""Task 14 v0.3 - MomentumV1."""
from __future__ import annotations
import hashlib, logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional
import numpy as np
from src.signals.schema import Signal, SignalAction, SignalBatch, SignalCategory, SignalStrength
from src.signals.strategies._base import PriceSeries, Strategy, TimeframeSpec
from src.signals.strategies._indicators import compute_macd, compute_obv_slope_normalized, compute_rsi, compute_zscore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MomentumV1Params:
    z_lookback_bars: int = 20
    z_threshold_weak: float = 0.5
    z_threshold_strong: float = 2.0
    macd_fast_period: int = 12
    macd_slow_period: int = 26
    macd_signal_period: int = 9
    macd_threshold_weak_pct: float = 0.0001
    macd_threshold_strong_pct: float = 0.0010
    rsi_period: int = 14
    rsi_buy_threshold: float = 55.0
    rsi_sell_threshold: float = 45.0
    rsi_strength_strong: float = 70.0
    volume_lookback_bars: int = 20
    volume_threshold_weak: float = 0.05
    volume_threshold_strong: float = 0.20
    price_weight: float = 0.70
    volume_weight: float = 0.30
    max_zero_volume_ratio: float = 0.30

    def __post_init__(self) -> None:
        if not (0 < self.z_threshold_weak < self.z_threshold_strong):
            raise ValueError("z thresholds")
        if not (0 < self.macd_threshold_weak_pct < self.macd_threshold_strong_pct):
            raise ValueError("macd thresholds")
        if not (50.0 < self.rsi_buy_threshold < self.rsi_strength_strong):
            raise ValueError("rsi buy")
        if not (100.0 - self.rsi_strength_strong < self.rsi_sell_threshold < 50.0):
            raise ValueError("rsi sell")
        if not (0 < self.volume_threshold_weak < self.volume_threshold_strong):
            raise ValueError("vol thresholds")
        if abs((self.price_weight + self.volume_weight) - 1.0) > 1e-9:
            raise ValueError("weights sum")
        if not (0 < self.price_weight < 1) or not (0 < self.volume_weight < 1):
            raise ValueError("weights range")
        if not (0 <= self.max_zero_volume_ratio < 1):
            raise ValueError("max zero vol")
        if self.z_lookback_bars <= 0 or self.volume_lookback_bars <= 0:
            raise ValueError("lookback")
        if self.macd_fast_period <= 0 or self.macd_slow_period <= 0:
            raise ValueError("macd periods")
        if self.macd_fast_period >= self.macd_slow_period:
            raise ValueError("macd order")
        if self.rsi_period <= 0:
            raise ValueError("rsi period")


class _Direction:
    BUY = "BUY"
    SELL = "SELL"
    NEUTRAL = "NEUTRAL"


class MomentumV1(Strategy):
    NAME = "momentum_v1"
    VERSION = "v1.0.0"

    def __init__(self, timeframe: TimeframeSpec, params: Optional[MomentumV1Params] = None) -> None:
        if not isinstance(timeframe, TimeframeSpec):
            raise TypeError("timeframe must be TimeframeSpec")
        self._timeframe = timeframe
        self._params = params if params is not None else MomentumV1Params()

    @property
    def name(self) -> str:
        return self.NAME

    @property
    def version(self) -> str:
        return self.VERSION

    @property
    def timeframe(self) -> TimeframeSpec:
        return self._timeframe

    @property
    def params(self) -> MomentumV1Params:
        return self._params

    @property
    def required_lookback_bars(self) -> int:
        p = self._params
        return max(p.z_lookback_bars + 1, p.macd_slow_period + p.macd_signal_period,
                   p.rsi_period + 1, p.volume_lookback_bars + 1)

    def evaluate(self, universe, as_of_utc):
        if as_of_utc.tzinfo is None:
            raise ValueError("as_of_utc must be tz-aware")
        as_of_utc = as_of_utc.astimezone(timezone.utc)
        signals = []
        si = sid = sag = shz = se = nc = pvd = 0
        for symbol, series in universe.items():
            try:
                outcome = self._evaluate_one(symbol, series, as_of_utc)
                if outcome is None:
                    nc += 1
                    continue
                if isinstance(outcome, str):
                    if outcome == "INSUFFICIENT": si += 1
                    elif outcome == "INVALID_DATA": sid += 1
                    elif outcome == "ABNORMAL_GAP": sag += 1
                    elif outcome == "HIGH_ZERO_VOLUME": shz += 1
                    elif outcome == "PRICE_VOLUME_DISAGREE": pvd += 1
                    elif outcome == "NO_CONSENSUS": nc += 1
                    continue
                signals.append(outcome)
            except Exception as exc:
                se += 1
                logger.warning("strategy %s skipped %s: %s", self.NAME, symbol, exc)
        meta = {
            "evaluated_count": len(universe), "signals_emitted": len(signals),
            "no_consensus_count": nc, "price_volume_disagreement_count": pvd,
            "skipped_insufficient_data": si, "skipped_invalid_data": sid,
            "skipped_abnormal_gap": sag, "skipped_high_zero_volume": shz,
            "skipped_exception": se,
            "timeframe_label": self._timeframe.label,
            "timeframe_bar_seconds": self._timeframe.bar_seconds,
        }
        return SignalBatch(
            strategy_name=self.NAME, strategy_version=self.VERSION,
            generated_at_utc=as_of_utc, signals=tuple(signals),
            universe_size=len(universe), metadata=meta,
        )

    def _evaluate_one(self, symbol, series, as_of_utc):
        p = self._params
        if len(series) < self.required_lookback_bars: return "INSUFFICIENT"
        if series.bar_seconds != self._timeframe.bar_seconds: return "INVALID_DATA"
        if series.has_abnormal_gaps(): return "ABNORMAL_GAP"
        closes_dec = series.closes()
        closes = np.array([float(c) for c in closes_dec], dtype=np.float64)
        volumes = np.array(series.volumes(), dtype=np.float64)
        if np.any(closes <= 0): return "INVALID_DATA"
        vol_window = volumes[-p.volume_lookback_bars:]
        zero_ratio = float(np.mean(vol_window <= 0))
        if zero_ratio > p.max_zero_volume_ratio: return "HIGH_ZERO_VOLUME"
        z = compute_zscore(closes, p.z_lookback_bars)
        macd_line, signal_line, hist = compute_macd(closes, p.macd_fast_period, p.macd_slow_period, p.macd_signal_period)
        rsi = compute_rsi(closes, p.rsi_period)
        obv_norm = compute_obv_slope_normalized(closes, volumes, p.volume_lookback_bars)
        avg_close = float(np.mean(closes[-p.macd_slow_period:]))
        macd_thr_weak = p.macd_threshold_weak_pct * avg_close
        z_dir = self._z_direction(z, p)
        macd_dir = self._macd_direction(macd_line, signal_line, hist, macd_thr_weak)
        rsi_dir = self._rsi_direction(rsi, p)
        vol_dir = self._volume_direction(obv_norm, p)
        price_dirs = (z_dir, macd_dir, rsi_dir)
        if all(d == _Direction.BUY for d in price_dirs): pc = _Direction.BUY
        elif all(d == _Direction.SELL for d in price_dirs): pc = _Direction.SELL
        else: return "NO_CONSENSUS"
        if vol_dir == _Direction.NEUTRAL: return "NO_CONSENSUS"
        if pc != vol_dir: return "PRICE_VOLUME_DISAGREE"
        if pc == _Direction.BUY:
            action = SignalAction.BUY; category = SignalCategory.ENTRY
        else:
            action = SignalAction.SELL; category = SignalCategory.EXIT
        z_conf = min(abs(z) / p.z_threshold_strong, 1.0)
        macd_thr_strong = p.macd_threshold_strong_pct * avg_close
        macd_conf = min(abs(hist) / max(macd_thr_strong, 1e-12), 1.0)
        rsi_conf = min(abs(rsi - 50.0) / max(p.rsi_strength_strong - 50.0, 1e-12), 1.0)
        avg_price_conf = (z_conf + macd_conf + rsi_conf) / 3.0
        vol_conf = min(abs(obv_norm) / p.volume_threshold_strong, 1.0)
        overall_conf = (avg_price_conf * p.price_weight) + (vol_conf * p.volume_weight)
        if overall_conf >= 0.75: strength = SignalStrength.STRONG
        elif overall_conf >= 0.40: strength = SignalStrength.MEDIUM
        else: strength = SignalStrength.WEAK
        inputs_hash = self._compute_inputs_hash(symbol, series, as_of_utc)
        metadata = {
            "z_score": round(z, 6), "macd_line": round(macd_line, 6),
            "macd_signal": round(signal_line, 6), "macd_histogram": round(hist, 6),
            "macd_threshold_weak": round(macd_thr_weak, 6),
            "rsi": round(rsi, 4), "obv_slope_normalized": round(obv_norm, 6),
            "z_confidence": round(z_conf, 4), "macd_confidence": round(macd_conf, 4),
            "rsi_confidence": round(rsi_conf, 4), "volume_confidence": round(vol_conf, 4),
            "overall_confidence": round(overall_conf, 4),
            "timeframe": self._timeframe.label,
            "lookback_bars_used": self.required_lookback_bars,
        }
        expires_at_utc = as_of_utc + self._timeframe.signal_validity
        return Signal(
            strategy_name=self.NAME, strategy_version=self.VERSION,
            symbol=symbol, action=action, strength=strength,
            signal_category=category, as_of_utc=as_of_utc,
            expires_at_utc=expires_at_utc,
            reference_price=series.latest_close(),
            confidence=Decimal(str(round(overall_conf, 6))),
            inputs_hash=inputs_hash, metadata=metadata,
        )

    @staticmethod
    def _z_direction(z, p):
        if z >= p.z_threshold_weak: return _Direction.BUY
        if z <= -p.z_threshold_weak: return _Direction.SELL
        return _Direction.NEUTRAL

    @staticmethod
    def _macd_direction(macd_line, signal_line, hist, threshold_weak):
        if hist > threshold_weak and macd_line > signal_line: return _Direction.BUY
        if hist < -threshold_weak and macd_line < signal_line: return _Direction.SELL
        return _Direction.NEUTRAL

    @staticmethod
    def _rsi_direction(rsi, p):
        if rsi > p.rsi_buy_threshold: return _Direction.BUY
        if rsi < p.rsi_sell_threshold: return _Direction.SELL
        return _Direction.NEUTRAL

    @staticmethod
    def _volume_direction(obv_norm, p):
        if obv_norm > p.volume_threshold_weak: return _Direction.BUY
        if obv_norm < -p.volume_threshold_weak: return _Direction.SELL
        return _Direction.NEUTRAL

    def _compute_inputs_hash(self, symbol, series, as_of_utc):
        p = self._params
        h = hashlib.sha256()
        def add(s):
            h.update(s.encode("utf-8"))
            h.update(b"|")
        add(self.NAME); add(self.VERSION)
        add(self._timeframe.label); add(str(self._timeframe.bar_seconds))
        add(symbol); add(as_of_utc.isoformat())
        for k in sorted(p.__dataclass_fields__.keys()):
            add(f"{k}={getattr(p, k)}")
        lookback = self.required_lookback_bars
        for bar in series.bars[-lookback:]:
            add(bar.timestamp_utc.isoformat())
            add(str(bar.close)); add(str(bar.volume))
        return h.hexdigest()[:32]


__all__ = ["MomentumV1", "MomentumV1Params"]
