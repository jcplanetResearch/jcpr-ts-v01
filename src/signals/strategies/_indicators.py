"""NumPy-based indicators for Task 14."""
from __future__ import annotations

from typing import Final
import numpy as np

_DTYPE: Final = np.float64


def _to_array(values) -> np.ndarray:
    arr = np.asarray(values, dtype=_DTYPE)
    if arr.ndim != 1:
        raise ValueError(f"expected 1D, got shape {arr.shape}")
    return arr


def compute_zscore(closes, lookback: int) -> float:
    if lookback <= 0:
        raise ValueError(f"lookback must be > 0, got {lookback}")
    arr = _to_array(closes)
    if arr.size < lookback + 1:
        raise ValueError(f"need >= {lookback + 1} closes, got {arr.size}")
    if np.any(arr <= 0):
        raise ValueError("all closes must be > 0")

    recent = arr[-(lookback + 1):]
    log_rets = np.log(recent[1:] / recent[:-1])
    cumulative = float(np.sum(log_rets))
    sigma = float(np.std(log_rets, ddof=0))
    if sigma <= 0.0:
        return 0.0
    return cumulative / (sigma * float(np.sqrt(lookback)))


def _ema(values: np.ndarray, period: int) -> np.ndarray:
    if period <= 0:
        raise ValueError(f"period must be > 0, got {period}")
    n = values.size
    if n < period:
        raise ValueError(f"need >= {period} values, got {n}")

    out = np.full(n, np.nan, dtype=_DTYPE)
    alpha = 2.0 / (period + 1.0)
    out[period - 1] = float(np.mean(values[:period]))
    for i in range(period, n):
        out[i] = alpha * values[i] + (1.0 - alpha) * out[i - 1]
    return out


def compute_macd(closes, fast_period=12, slow_period=26, signal_period=9):
    if fast_period <= 0 or slow_period <= 0 or signal_period <= 0:
        raise ValueError("all periods must be > 0")
    if fast_period >= slow_period:
        raise ValueError("fast_period < slow_period")
    arr = _to_array(closes)
    min_len = slow_period + signal_period
    if arr.size < min_len:
        raise ValueError(f"need >= {min_len} closes, got {arr.size}")
    if np.any(arr <= 0):
        raise ValueError("all closes must be > 0")

    ema_fast = _ema(arr, fast_period)
    ema_slow = _ema(arr, slow_period)
    macd_line_full = ema_fast - ema_slow

    valid_start = slow_period - 1
    macd_valid = macd_line_full[valid_start:]
    if macd_valid.size < signal_period:
        raise ValueError("insufficient MACD points")
    signal_full = _ema(macd_valid, signal_period)

    macd_last = float(macd_line_full[-1])
    signal_last = float(signal_full[-1])
    return macd_last, signal_last, macd_last - signal_last


def compute_rsi(closes, period: int = 14) -> float:
    if period <= 0:
        raise ValueError(f"period must be > 0, got {period}")
    arr = _to_array(closes)
    if arr.size < period + 1:
        raise ValueError(f"need >= {period + 1} closes, got {arr.size}")
    if np.any(arr <= 0):
        raise ValueError("all closes must be > 0")

    deltas = np.diff(arr)
    gains = np.where(deltas > 0, deltas, 0.0).astype(_DTYPE)
    losses = np.where(deltas < 0, -deltas, 0.0).astype(_DTYPE)

    avg_gain = float(np.mean(gains[:period]))
    avg_loss = float(np.mean(losses[:period]))
    for i in range(period, deltas.size):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss <= 0.0:
        return 100.0 if avg_gain > 0.0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def compute_obv(closes, volumes) -> np.ndarray:
    closes_arr = _to_array(closes)
    volumes_arr = _to_array(volumes)
    if closes_arr.size != volumes_arr.size:
        raise ValueError("closes and volumes must have equal length")
    if closes_arr.size < 2:
        raise ValueError("need >= 2 bars for OBV")
    if np.any(closes_arr <= 0):
        raise ValueError("all closes must be > 0")
    if np.any(volumes_arr < 0):
        raise ValueError("all volumes must be >= 0")

    obv = np.zeros(closes_arr.size, dtype=_DTYPE)
    diffs = np.diff(closes_arr)
    for i, d in enumerate(diffs, start=1):
        if d > 0:
            obv[i] = obv[i-1] + volumes_arr[i]
        elif d < 0:
            obv[i] = obv[i-1] - volumes_arr[i]
        else:
            obv[i] = obv[i-1]
    return obv


def compute_obv_slope_normalized(closes, volumes, lookback: int) -> float:
    if lookback < 2:
        raise ValueError(f"lookback must be >= 2, got {lookback}")
    closes_arr = _to_array(closes)
    volumes_arr = _to_array(volumes)
    if closes_arr.size < lookback + 1:
        raise ValueError(f"need >= {lookback + 1} bars, got {closes_arr.size}")

    obv = compute_obv(closes_arr, volumes_arr)
    obv_window = obv[-lookback:]
    vol_window = volumes_arr[-lookback:]

    avg_vol = float(np.mean(vol_window))
    if avg_vol <= 0.0:
        return 0.0

    x = np.arange(lookback, dtype=_DTYPE)
    coeffs = np.polyfit(x, obv_window, deg=1)
    return float(coeffs[0]) / avg_vol


__all__ = [
    "compute_zscore",
    "compute_macd",
    "compute_rsi",
    "compute_obv",
    "compute_obv_slope_normalized",
]
