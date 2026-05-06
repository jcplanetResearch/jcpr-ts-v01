"""
가격 합리성 점검 게이트 v2 (Price Sanity Gate v2)
==================================================

JCPR Trading System - jcpr-ts-v01
Task 19 보강 — Task 13 (Quote) 통합

이전 버전 → v0.4 변경 (Changes):
- QuoteStore 통합 옵션: 호가 스냅샷의 mid-quote 사용 가능
- 신선도 (staleness) 검증: max_age_sec 초과 호가 거부
- 하위 호환 (backward compatible): QuoteStore 없으면 ctx.last_quote_price 폴백

원칙 (Principles):
- fail-closed: 호가 stale 또는 부재 시 거부
- 우선순위: QuoteStore (있으면 + 신선) → ctx.last_quote_price (폴백)
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from .base import GateResult, RiskContext, RiskGate


class PriceSanityGate(RiskGate):
    """
    지정가가 기준가 대비 ±X% 벗어나면 거부.
    (Reject limit price if it deviates from reference by ±X%.)

    기준가 우선순위 (Reference price priority):
    1. quote_store가 주어지면 → mid-quote (best_bid + best_ask) / 2
       단, 신선도 (max_quote_age_sec) 통과 필요
    2. 폴백: ctx.last_quote_price
    """

    name = "price_sanity"

    def __init__(
        self,
        max_deviation_pct: Decimal = Decimal("0.05"),
        *,
        quote_store=None,                     # QuoteStore (Task 13) — 옵션
        max_quote_age_sec: int = 30,          # 호가 신선도 임계 (초)
        prefer_mid_quote: bool = True,        # mid-quote 우선 사용
    ):
        if max_deviation_pct <= 0 or max_deviation_pct > 1:
            raise ValueError("max_deviation_pct는 (0,1] 범위")
        if max_quote_age_sec <= 0:
            raise ValueError("max_quote_age_sec는 양수")
        self._max_dev = max_deviation_pct
        self._quote_store = quote_store
        self._max_quote_age_sec = max_quote_age_sec
        self._prefer_mid = prefer_mid_quote

    def evaluate(self, ctx: RiskContext) -> GateResult:
        ref_price, ref_source, detail_extra = self._resolve_reference_price(ctx)

        if ref_price is None:
            return GateResult(
                gate_name=self.name, outcome="reject",
                reason="기준가 없음 또는 stale (no/stale reference, fail-closed)",
                detail={"symbol": ctx.symbol, "ref_source": ref_source, **detail_extra},
            )
        if ref_price <= 0:
            return GateResult(
                gate_name=self.name, outcome="reject",
                reason=f"기준가 비정상 (invalid reference): {ref_price}",
                detail={"ref_source": ref_source, **detail_extra},
            )

        deviation = abs(ctx.price - ref_price) / ref_price
        if deviation > self._max_dev:
            return GateResult(
                gate_name=self.name, outcome="reject",
                reason=f"가격 편차 한도 초과 (price deviation exceeded): "
                       f"{deviation:.4%} > {self._max_dev:.4%}",
                detail={
                    "order_price": str(ctx.price),
                    "ref_price": str(ref_price),
                    "ref_source": ref_source,
                    "deviation": f"{deviation:.6f}",
                    "max": f"{self._max_dev:.6f}",
                    **detail_extra,
                },
            )
        return GateResult(
            gate_name=self.name, outcome="pass", reason=None,
            detail={"ref_source": ref_source, "ref_price": str(ref_price)},
        )

    def _resolve_reference_price(
        self, ctx: RiskContext,
    ) -> tuple[Optional[Decimal], str, dict]:
        """
        기준가 결정.
        Returns: (ref_price, source_label, detail_dict)
        """
        # 1. QuoteStore + mid-quote (있고 prefer 시)
        if self._quote_store is not None and self._prefer_mid:
            try:
                snap = self._quote_store.latest_fresh(
                    ctx.symbol, ctx.market_now_utc, self._max_quote_age_sec,
                )
            except Exception as e:  # noqa: BLE001 - any store error → fallback
                snap = None

            if snap is not None:
                mid = snap.mid_quote()
                age_sec = snap.age_seconds(ctx.market_now_utc)
                return (
                    mid,
                    f"quote_store_mid({snap.source})",
                    {
                        "best_bid": str(snap.best_bid),
                        "best_ask": str(snap.best_ask),
                        "quote_age_sec": f"{age_sec:.2f}",
                        "is_live_source": snap.is_live_source,
                    },
                )
            # snap=None: stale이거나 없음 → 폴백 시도

        # 2. 폴백: ctx.last_quote_price
        if ctx.last_quote_price is not None:
            return (
                ctx.last_quote_price,
                "ctx_last_quote",
                {"note": "fallback to ctx.last_quote_price"},
            )

        return None, "none", {"note": "no reference price available"}
