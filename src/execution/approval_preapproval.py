"""
사전 승인 (Pre-Approval)
=========================

JCPR Trading System - jcpr-ts-v01
Task 40 v0.1

운영자가 세션 시작 시 한도(범위)를 사전 승인 → 그 안에서는 자동 통과.
한도 초과 시 다음 Provider(예: CLI)로 폴백.

원칙 (Principles):
- 윈도우는 메모리에만 (재시작 시 손실 — 의도적 안전)
- 명시적 한도: 종목/방향/수량/금액/횟수/유효기간
- Revoke 가능 (운영자가 즉시 무효화)
- 모든 사용 audit log
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
from uuid import uuid4

from .approval import ApprovalDecision, ApprovalProvider, ApprovalRequest
from .approval_audit import ApprovalAuditLog

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────
# Approval Window
# ─────────────────────────────────────────────────

@dataclass(frozen=True)
class ApprovalWindow:
    """
    사전 승인 윈도우.
    (Pre-approval window — defines auto-approve limits.)
    """
    window_id: str
    reason: str                                       # 운영자 메모 (audit)
    created_at_utc: datetime
    valid_until_utc: datetime
    max_orders: int                                   # 이 윈도우 내 허용 주문 수
    used_orders: int = 0                              # 사용된 주문 수
    revoked: bool = False

    # 매칭 조건 (None이면 "임의" — 모든 값 허용)
    symbol: Optional[str] = None
    side: Optional[str] = None
    max_quantity_per_order: Optional[int] = None
    max_total_cost_per_order_krw: Optional[Decimal] = None

    # 환경 제한
    allow_live_env: bool = False                     # True면 KIS_ENV=live에서도 허용

    def matches(self, request: ApprovalRequest, *, now_utc: datetime) -> tuple[bool, str]:
        """
        request가 이 윈도우와 매칭되는지.
        Returns: (matches, reason_if_not)
        """
        if self.revoked:
            return False, "window revoked"
        if now_utc >= self.valid_until_utc:
            return False, f"window expired at {self.valid_until_utc.isoformat()}"
        if self.used_orders >= self.max_orders:
            return False, f"window exhausted ({self.used_orders}/{self.max_orders} used)"

        if self.symbol is not None and self.symbol != request.symbol:
            return False, f"symbol mismatch: window={self.symbol}, req={request.symbol}"
        if self.side is not None and self.side != request.side:
            return False, f"side mismatch: window={self.side}, req={request.side}"

        if (
            self.max_quantity_per_order is not None
            and request.quantity > self.max_quantity_per_order
        ):
            return False, (
                f"quantity exceeds limit: req={request.quantity} > "
                f"max={self.max_quantity_per_order}"
            )
        if (
            self.max_total_cost_per_order_krw is not None
            and request.estimated_cost_krw > self.max_total_cost_per_order_krw
        ):
            return False, (
                f"cost exceeds limit: req={request.estimated_cost_krw} > "
                f"max={self.max_total_cost_per_order_krw}"
            )

        # 환경 제한
        if request.is_live_env and not request.is_dry_run and not self.allow_live_env:
            return False, "live env + live orders requires allow_live_env=True"

        return True, ""

    def with_used_increment(self) -> "ApprovalWindow":
        return replace(self, used_orders=self.used_orders + 1)

    def with_revoked(self) -> "ApprovalWindow":
        return replace(self, revoked=True)

    def to_dict(self) -> dict:
        return {
            "window_id": self.window_id,
            "reason": self.reason,
            "created_at_utc": self.created_at_utc.isoformat(),
            "valid_until_utc": self.valid_until_utc.isoformat(),
            "max_orders": self.max_orders,
            "used_orders": self.used_orders,
            "revoked": self.revoked,
            "symbol": self.symbol,
            "side": self.side,
            "max_quantity_per_order": self.max_quantity_per_order,
            "max_total_cost_per_order_krw": (
                str(self.max_total_cost_per_order_krw)
                if self.max_total_cost_per_order_krw is not None else None
            ),
            "allow_live_env": self.allow_live_env,
        }


# ─────────────────────────────────────────────────
# Pre-Approval Manager
# ─────────────────────────────────────────────────

class PreApprovalManager:
    """
    사전 승인 윈도우 관리.
    (Pre-approval window manager — in-memory only.)
    """

    def __init__(self):
        self._windows: dict[str, ApprovalWindow] = {}
        self._lock = threading.Lock()

    def add_window(
        self,
        *,
        max_orders: int,
        valid_for_seconds: int,
        reason: str,
        symbol: Optional[str] = None,
        side: Optional[str] = None,
        max_quantity_per_order: Optional[int] = None,
        max_total_cost_per_order_krw: Optional[Decimal] = None,
        allow_live_env: bool = False,
    ) -> str:
        """
        새 사전 승인 윈도우 추가.

        Returns: window_id
        """
        if not reason or not reason.strip():
            raise ValueError("reason 필수 (audit log)")
        if max_orders <= 0:
            raise ValueError(f"max_orders 양수 필요: {max_orders}")
        if valid_for_seconds <= 0:
            raise ValueError(f"valid_for_seconds 양수 필요: {valid_for_seconds}")
        if side is not None and side not in ("buy", "sell"):
            raise ValueError(f"side는 'buy' 또는 'sell': {side!r}")
        if max_quantity_per_order is not None and max_quantity_per_order <= 0:
            raise ValueError("max_quantity_per_order 양수 필요")
        if (
            max_total_cost_per_order_krw is not None
            and max_total_cost_per_order_krw <= 0
        ):
            raise ValueError("max_total_cost_per_order_krw 양수 필요")

        from datetime import timedelta
        now = datetime.now(timezone.utc)
        window = ApprovalWindow(
            window_id=f"window-{uuid4().hex[:12]}",
            reason=reason.strip(),
            created_at_utc=now,
            valid_until_utc=now + timedelta(seconds=valid_for_seconds),
            max_orders=max_orders,
            symbol=symbol,
            side=side,
            max_quantity_per_order=max_quantity_per_order,
            max_total_cost_per_order_krw=max_total_cost_per_order_krw,
            allow_live_env=allow_live_env,
        )
        with self._lock:
            self._windows[window.window_id] = window
        logger.info(
            "사전 승인 윈도우 추가: id=%s symbol=%s side=%s max_orders=%d valid=%ds reason=%r",
            window.window_id, symbol, side, max_orders, valid_for_seconds, reason,
        )
        return window.window_id

    def revoke(self, window_id: str) -> bool:
        """윈도우 무효화. Returns: True if revoked, False if not found."""
        with self._lock:
            w = self._windows.get(window_id)
            if w is None:
                return False
            self._windows[window_id] = w.with_revoked()
        logger.info("사전 승인 윈도우 revoked: id=%s", window_id)
        return True

    def revoke_all(self) -> int:
        """모든 활성 윈도우 무효화. Returns: revoked count."""
        with self._lock:
            count = 0
            for wid, w in list(self._windows.items()):
                if not w.revoked:
                    self._windows[wid] = w.with_revoked()
                    count += 1
        logger.info("모든 사전 승인 윈도우 revoked: count=%d", count)
        return count

    def find_matching_window(
        self, request: ApprovalRequest, *, now_utc: Optional[datetime] = None,
    ) -> Optional[ApprovalWindow]:
        """request에 매칭되는 윈도우 검색 (가장 좁은 범위 우선)."""
        if now_utc is None:
            now_utc = datetime.now(timezone.utc)

        with self._lock:
            candidates = []
            for w in self._windows.values():
                ok, _ = w.matches(request, now_utc=now_utc)
                if ok:
                    candidates.append(w)

        if not candidates:
            return None

        # 가장 좁은 범위 우선 — symbol 지정 + side 지정 > symbol만 > 일반
        def specificity(w: ApprovalWindow) -> int:
            score = 0
            if w.symbol is not None:
                score += 4
            if w.side is not None:
                score += 2
            if w.max_quantity_per_order is not None:
                score += 1
            return score

        candidates.sort(key=specificity, reverse=True)
        return candidates[0]

    def consume(self, window_id: str) -> bool:
        """
        윈도우의 used_orders 증가. (실제 사용 후 호출)
        Returns: True if consumed, False if not found / already exhausted.
        """
        with self._lock:
            w = self._windows.get(window_id)
            if w is None:
                return False
            if w.revoked or w.used_orders >= w.max_orders:
                return False
            self._windows[window_id] = w.with_used_increment()
        return True

    def get_all(self, *, only_active: bool = False) -> dict[str, ApprovalWindow]:
        """현재 윈도우 dict 반환."""
        now = datetime.now(timezone.utc)
        with self._lock:
            if only_active:
                return {
                    wid: w for wid, w in self._windows.items()
                    if not w.revoked
                    and now < w.valid_until_utc
                    and w.used_orders < w.max_orders
                }
            return dict(self._windows)


# ─────────────────────────────────────────────────
# PreApprovalProvider
# ─────────────────────────────────────────────────

class PreApprovalProvider(ApprovalProvider):
    """
    사전 승인 윈도우 기반 ApprovalProvider.

    매칭되는 윈도우가 있으면 자동 승인 (used_orders 증가).
    매칭 없으면 거부 (다음 Provider로 폴백 — Composite에서 처리).
    """

    name = "pre_approval"

    def __init__(
        self,
        manager: PreApprovalManager,
        *,
        audit_log: Optional[ApprovalAuditLog] = None,
    ):
        self._manager = manager
        self._audit_log = audit_log

    def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        now = datetime.now(timezone.utc)
        window = self._manager.find_matching_window(request, now_utc=now)

        if window is None:
            decision = ApprovalDecision(
                approved=False,
                reason="no matching pre-approval window",
                decided_at_utc=now,
                approver=self.name,
            )
            self._audit(request, decision, window_id=None)
            return decision

        # 윈도우 사용
        consumed = self._manager.consume(window.window_id)
        if not consumed:
            # 동시성 — 다른 호출자가 먼저 사용
            decision = ApprovalDecision(
                approved=False,
                reason=f"window race condition: {window.window_id}",
                decided_at_utc=now,
                approver=self.name,
            )
            self._audit(request, decision, window_id=window.window_id)
            return decision

        decision = ApprovalDecision(
            approved=True,
            reason=(
                f"pre-approved by window {window.window_id} "
                f"(used {window.used_orders + 1}/{window.max_orders}): {window.reason}"
            ),
            decided_at_utc=now,
            approver=self.name,
            metadata={"window_id": window.window_id},
        )
        self._audit(request, decision, window_id=window.window_id)
        return decision

    def _audit(
        self,
        request: ApprovalRequest,
        decision: ApprovalDecision,
        *,
        window_id: Optional[str],
    ) -> None:
        if self._audit_log is None:
            return
        try:
            self._audit_log.write(
                request, decision,
                response_time_sec=0.0,
                provider_chain=[self.name],
                extra={"window_id": window_id} if window_id else None,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Pre-approval audit 기록 실패: %s", e)
