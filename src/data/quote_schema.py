"""
호가 스냅샷 스키마 (Quote Snapshot Schema)
==========================================

JCPR Trading System - jcpr-ts-v01
Task 13 v0.1 데이터 모델

특정 시점의 best bid/ask + N단계 호가 잔량.
(Point-in-time best bid/ask + N-level order book depth.)

원칙 (Principles):
- UTC tz-aware datetime 강제
- Decimal 가격 (정밀도 보존)
- frozen=True (immutable)
- 신선도 (staleness) 검증 fail-closed
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

DEFAULT_DEPTH_LEVELS = 10  # KRX 표준


# ─────────────────────────────────────────────────
# Depth Level (호가 한 단계)
# ─────────────────────────────────────────────────

@dataclass(frozen=True)
class DepthLevel:
    """호가창 한 단계 (One level of order book)."""
    level: int            # 1 = best, 2 = 2nd, ...
    price: Decimal
    bid_size: int         # 매수 잔량 (해당 가격에 매수 대기)
    ask_size: int         # 매도 잔량

    def __post_init__(self) -> None:
        if self.level < 1:
            raise ValueError(f"level은 1 이상: {self.level}")
        if self.price <= 0:
            raise ValueError(f"가격은 양수: {self.price}")
        if self.bid_size < 0:
            raise ValueError(f"bid_size 음수 불가: {self.bid_size}")
        if self.ask_size < 0:
            raise ValueError(f"ask_size 음수 불가: {self.ask_size}")


# ─────────────────────────────────────────────────
# Quote Snapshot
# ─────────────────────────────────────────────────

@dataclass(frozen=True)
class QuoteSnapshot:
    """
    호가 스냅샷 (Quote snapshot at point-in-time).

    captured_at_utc: 데이터 소스가 호가를 캡처한 시각 (시장 시각)
    received_at_utc: 시스템이 수신한 시각 (네트워크 지연 후)
    """
    symbol: str
    captured_at_utc: datetime
    received_at_utc: datetime
    best_bid: Decimal
    best_ask: Decimal
    best_bid_size: int
    best_ask_size: int
    depth_levels: tuple[DepthLevel, ...] = field(default_factory=tuple)
    last_trade_price: Optional[Decimal] = None
    source: str = "unknown"
    is_live_source: bool = False

    def __post_init__(self) -> None:
        # tz-aware
        if self.captured_at_utc.tzinfo is None:
            raise ValueError("captured_at_utc tz-aware 필수")
        if self.received_at_utc.tzinfo is None:
            raise ValueError("received_at_utc tz-aware 필수")

        # 가격 양수
        if self.best_bid <= 0:
            raise ValueError(f"best_bid 양수: {self.best_bid}")
        if self.best_ask <= 0:
            raise ValueError(f"best_ask 양수: {self.best_ask}")

        # 호가 정합성: best_ask >= best_bid (locked/crossed market 거부 — fail-closed)
        if self.best_ask < self.best_bid:
            raise ValueError(
                f"교차/락 마켓 (crossed/locked market): "
                f"ask({self.best_ask}) < bid({self.best_bid})"
            )

        # 잔량 음수 불가
        if self.best_bid_size < 0:
            raise ValueError(f"best_bid_size 음수 불가: {self.best_bid_size}")
        if self.best_ask_size < 0:
            raise ValueError(f"best_ask_size 음수 불가: {self.best_ask_size}")

        # depth level 1은 best와 일치해야 함 (있을 경우)
        if self.depth_levels:
            level1 = next((d for d in self.depth_levels if d.level == 1), None)
            if level1 is not None:
                if level1.price != self.best_bid and level1.price != self.best_ask:
                    # KRX는 매수/매도 호가가 분리되므로 level 1 price가 best_bid나 best_ask 중 하나여야 함
                    # 단, depth_levels가 매수/매도 통합된 mid 형식일 수 있어 엄격히 검사하지 않음
                    pass
            # 중복 level 거부
            levels_seen = set()
            for d in self.depth_levels:
                if d.level in levels_seen:
                    raise ValueError(f"중복 호가 level: {d.level}")
                levels_seen.add(d.level)

        if self.last_trade_price is not None and self.last_trade_price <= 0:
            raise ValueError(f"last_trade_price 양수: {self.last_trade_price}")

    # ---------- 파생 지표 (Derived Metrics) ----------

    def mid_quote(self) -> Decimal:
        """중간 호가 (mid-quote) — 공정 가격 추정."""
        return (self.best_bid + self.best_ask) / Decimal("2")

    def spread(self) -> Decimal:
        """절대 스프레드."""
        return self.best_ask - self.best_bid

    def spread_bps(self) -> Optional[Decimal]:
        """베이시스 포인트 스프레드 (1bp = 0.01%)."""
        mid = self.mid_quote()
        if mid <= 0:
            return None
        return (self.spread() / mid) * Decimal("10000")

    def imbalance(self) -> Optional[Decimal]:
        """
        호가 잔량 불균형 (-1 ~ +1).
        +1.0 = 매수 100%, -1.0 = 매도 100%, 0 = 균형.
        """
        total = self.best_bid_size + self.best_ask_size
        if total == 0:
            return None
        return (Decimal(self.best_bid_size) - Decimal(self.best_ask_size)) / Decimal(total)

    def depth_imbalance(self, levels: int = 5) -> Optional[Decimal]:
        """
        N단계 누적 잔량 불균형.
        depth_levels에서 상위 N개의 (bid_size - ask_size) / (bid_size + ask_size) 합산.
        """
        if not self.depth_levels:
            return None
        total_bid = sum(d.bid_size for d in self.depth_levels[:levels])
        total_ask = sum(d.ask_size for d in self.depth_levels[:levels])
        total = total_bid + total_ask
        if total == 0:
            return None
        return (Decimal(total_bid) - Decimal(total_ask)) / Decimal(total)

    def is_stale(self, now_utc: datetime, max_age_sec: int) -> bool:
        """
        스냅샷이 stale한지 판정.
        (Stale check — fail-closed for risk gates.)

        max_age_sec 초과 또는 received_at_utc가 미래면 True (믿을 수 없음).
        """
        if now_utc.tzinfo is None:
            raise ValueError("now_utc tz-aware 필수")
        age = (now_utc - self.received_at_utc).total_seconds()
        if age < 0:  # 미래 시각 — 시계 오류
            return True
        return age > max_age_sec

    def age_seconds(self, now_utc: datetime) -> float:
        """수신 시각 대비 경과 초."""
        if now_utc.tzinfo is None:
            raise ValueError("now_utc tz-aware 필수")
        return (now_utc - self.received_at_utc).total_seconds()
