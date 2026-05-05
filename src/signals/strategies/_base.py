"""Task 14 v0.3 base."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional


@dataclass(frozen=True)
class TimeframeSpec:
    bar_seconds: int
    label: str
    signal_validity: timedelta

    def __post_init__(self) -> None:
        if self.bar_seconds <= 0:
            raise ValueError(f"bar_seconds must be > 0, got {self.bar_seconds}")
        if not self.label:
            raise ValueError("label must not be empty")
        if self.signal_validity <= timedelta(0):
            raise ValueError("signal_validity must be positive")


TIMEFRAMES: dict[str, TimeframeSpec] = {
    "M1":  TimeframeSpec(bar_seconds=60,      label="1m",  signal_validity=timedelta(minutes=5)),
    "M3":  TimeframeSpec(bar_seconds=180,     label="3m",  signal_validity=timedelta(minutes=15)),
    "M5":  TimeframeSpec(bar_seconds=300,     label="5m",  signal_validity=timedelta(minutes=15)),
    "M15": TimeframeSpec(bar_seconds=900,     label="15m", signal_validity=timedelta(minutes=30)),
    "M30": TimeframeSpec(bar_seconds=1800,    label="30m", signal_validity=timedelta(hours=1)),
    "H1":  TimeframeSpec(bar_seconds=3600,    label="1h",  signal_validity=timedelta(hours=2)),
    "D1":  TimeframeSpec(bar_seconds=86400,   label="1d",  signal_validity=timedelta(days=1)),
    "W1":  TimeframeSpec(bar_seconds=604800,  label="1w",  signal_validity=timedelta(weeks=1)),
    "MO1": TimeframeSpec(bar_seconds=2592000, label="1mo", signal_validity=timedelta(days=30)),
}


@dataclass(frozen=True)
class PriceBar:
    timestamp_utc: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    bar_seconds: int = 86400

    def __post_init__(self) -> None:
        if self.timestamp_utc.tzinfo is None:
            raise ValueError("timestamp_utc must be tz-aware")
        for name, val in [("open", self.open), ("high", self.high),
                          ("low", self.low), ("close", self.close)]:
            if not isinstance(val, Decimal):
                raise TypeError(f"{name} must be Decimal")
            if val <= 0:
                raise ValueError(f"{name} must be > 0")
        if self.high < self.low:
            raise ValueError("high < low")
        if not (self.low <= self.open <= self.high):
            raise ValueError("open outside [low, high]")
        if not (self.low <= self.close <= self.high):
            raise ValueError("close outside [low, high]")
        if self.volume < 0:
            raise ValueError("volume must be >= 0")
        if self.bar_seconds <= 0:
            raise ValueError(f"bar_seconds must be > 0, got {self.bar_seconds}")


_KRX_DAILY_OVERNIGHT_SEC = 17 * 3600 + 30 * 60
_KRX_WEEKEND_OVERNIGHT_SEC = 65 * 3600 + 30 * 60
_OVERNIGHT_TOLERANCE_SEC = 4 * 3600


@dataclass(frozen=True)
class PriceSeries:
    symbol: str
    bars: tuple[PriceBar, ...]

    def __post_init__(self) -> None:
        if not self.symbol or not self.symbol.strip():
            raise ValueError("symbol must not be empty")
        if not self.bars:
            raise ValueError("bars must not be empty")

        first_bs = self.bars[0].bar_seconds
        prev_ts: Optional[datetime] = None
        for i, bar in enumerate(self.bars):
            if bar.bar_seconds != first_bs:
                raise ValueError(
                    f"bar #{i} has bar_seconds={bar.bar_seconds} but expected {first_bs} "
                    f"(all bars must share bar_seconds)"
                )
            if prev_ts is not None and bar.timestamp_utc <= prev_ts:
                raise ValueError(f"bar #{i} timestamp not after previous")
            prev_ts = bar.timestamp_utc

    @property
    def bar_seconds(self) -> int:
        return self.bars[0].bar_seconds

    def closes(self) -> tuple[Decimal, ...]:
        return tuple(b.close for b in self.bars)

    def volumes(self) -> tuple[int, ...]:
        return tuple(b.volume for b in self.bars)

    def latest_close(self) -> Decimal:
        return self.bars[-1].close

    def latest_timestamp(self) -> datetime:
        return self.bars[-1].timestamp_utc

    def __len__(self) -> int:
        return len(self.bars)

    def is_normal_gap(self, gap_seconds: float) -> bool:
        bs = self.bar_seconds
        if abs(gap_seconds - bs) < (bs * 0.1):
            return True
        if abs(gap_seconds - _KRX_DAILY_OVERNIGHT_SEC) < _OVERNIGHT_TOLERANCE_SEC:
            return True
        if abs(gap_seconds - _KRX_WEEKEND_OVERNIGHT_SEC) < _OVERNIGHT_TOLERANCE_SEC:
            return True
        if bs >= 86400 and bs <= gap_seconds <= bs * 5:
            return True
        return False

    def gaps(self) -> tuple[tuple[int, float, bool], ...]:
        result = []
        for i in range(1, len(self.bars)):
            gap = (self.bars[i].timestamp_utc - self.bars[i-1].timestamp_utc).total_seconds()
            result.append((i, gap, self.is_normal_gap(gap)))
        return tuple(result)

    def has_abnormal_gaps(self) -> bool:
        return any(not is_norm for _, _, is_norm in self.gaps())


class Strategy(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def version(self) -> str: ...

    @property
    @abstractmethod
    def required_lookback_bars(self) -> int: ...

    @property
    @abstractmethod
    def timeframe(self) -> TimeframeSpec: ...

    @abstractmethod
    def evaluate(self, universe, as_of_utc): ...


__all__ = ["TIMEFRAMES", "TimeframeSpec", "PriceBar", "PriceSeries", "Strategy"]
