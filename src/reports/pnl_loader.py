"""
P&L 로더 (P&L Loader)
======================

JCPR Trading System - jcpr-ts-v01
Task 49 v0.2 — Task 48 대시보드와 동일한 데이터 모델 사용.

SQLite DB에서 P&L 스냅샷 직접 계산 — Task 26 PnLEngine 객체 의존 제거.
(Computes P&L snapshot directly from SQLite DB without requiring PnLEngine
instance — same approach as Task 48 dashboard for consistency.)

Decimal로 일관 처리하여 직렬화 시 정밀도 유지.
(Uses Decimal throughout for serialization precision.)
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional


# ─────────────────────────────────────────────────
# SQLite 헬퍼 (read-only, mode=ro)
# ─────────────────────────────────────────────────

def _safe_query(
    db_path: Optional[str],
    query: str,
    params: tuple = (),
) -> list[dict[str, Any]]:
    """안전 쿼리 — 파일/테이블 없으면 빈 리스트."""
    if not db_path or not Path(db_path).exists():
        return []
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(query, params)
            return [dict(r) for r in cur.fetchall()]
    except sqlite3.Error:
        return []


def _table_exists(db_path: Optional[str], table: str) -> bool:
    """테이블 존재 여부."""
    if not db_path or not Path(db_path).exists():
        return False
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            )
            return cur.fetchone() is not None
    except sqlite3.Error:
        return False


def _get_mark_price(
    symbol: str,
    ohlcv_db: Optional[str],
    quote_db: Optional[str],
) -> Optional[Decimal]:
    """심볼의 mark price — quote 우선, OHLCV 종가 fallback."""
    if quote_db and _table_exists(quote_db, "quotes"):
        rows = _safe_query(
            quote_db,
            """
            SELECT (bid_krw + ask_krw) / 2.0 AS mid
            FROM quotes
            WHERE symbol = ?
            ORDER BY ts_utc DESC
            LIMIT 1
            """,
            (symbol,),
        )
        if rows and rows[0].get("mid") is not None:
            return Decimal(str(rows[0]["mid"]))

    if ohlcv_db and _table_exists(ohlcv_db, "ohlcv_daily"):
        rows = _safe_query(
            ohlcv_db,
            """
            SELECT close_krw
            FROM ohlcv_daily
            WHERE symbol = ?
            ORDER BY date DESC
            LIMIT 1
            """,
            (symbol,),
        )
        if rows and rows[0].get("close_krw") is not None:
            return Decimal(str(rows[0]["close_krw"]))

    return None


# ─────────────────────────────────────────────────
# P&L 스냅샷 (P&L Snapshot)
# ─────────────────────────────────────────────────

def compute_pnl_snapshot(
    *,
    positions_db: Optional[str],
    ohlcv_db: Optional[str],
    quote_db: Optional[str],
    starting_capital_krw: Decimal,
    cash_krw: Decimal,
    session_start_iso: Optional[str] = None,
    session_end_iso: Optional[str] = None,
) -> dict[str, Any]:
    """
    SQLite DB에서 P&L 스냅샷 계산.

    Returns:
        dict with keys (모두 Decimal):
            starting_capital_krw, cash_krw,
            position_market_value_krw, total_equity_krw,
            realized_pnl_krw, unrealized_pnl_krw, total_pnl_krw,
            total_return_pct,
            total_fees_krw, total_taxes_krw, total_slippage_krw,
            symbol_attribution: list of dict
            strategy_attribution: list of dict
    """
    starting = Decimal(str(starting_capital_krw))
    cash = Decimal(str(cash_krw))

    # ─── 포지션 (Positions) ────────────────────
    pos_rows: list[dict[str, Any]] = []
    if _table_exists(positions_db, "positions"):
        pos_rows = _safe_query(
            positions_db,
            """
            SELECT symbol, quantity, avg_cost_krw, side
            FROM positions
            WHERE quantity != 0
            ORDER BY symbol
            """,
        )

    # ─── 체결 (Fills) ──────────────────────────
    fill_rows: list[dict[str, Any]] = []
    if _table_exists(positions_db, "fills"):
        if session_start_iso and session_end_iso:
            fill_rows = _safe_query(
                positions_db,
                """
                SELECT fill_id, symbol, side, quantity, price_krw,
                       gross_krw, fee_krw, tax_krw, filled_at_utc,
                       strategy_id, intended_price_krw
                FROM fills
                WHERE filled_at_utc >= ? AND filled_at_utc <= ?
                """,
                (session_start_iso, session_end_iso),
            )
        else:
            fill_rows = _safe_query(
                positions_db,
                """
                SELECT fill_id, symbol, side, quantity, price_krw,
                       gross_krw, fee_krw, tax_krw, filled_at_utc,
                       strategy_id, intended_price_krw
                FROM fills
                """,
            )

    total_fees = sum((Decimal(str(r.get("fee_krw") or 0)) for r in fill_rows), Decimal(0))
    total_taxes = sum((Decimal(str(r.get("tax_krw") or 0)) for r in fill_rows), Decimal(0))

    # ─── 슬리피지 (Slippage) ───────────────────
    total_slippage = Decimal(0)
    for r in fill_rows:
        intended = r.get("intended_price_krw")
        actual = r.get("price_krw")
        qty = r.get("quantity")
        side = r.get("side")
        if intended is None or actual is None or qty is None or side is None:
            continue
        try:
            d_intended = Decimal(str(intended))
            d_actual = Decimal(str(actual))
            d_qty = Decimal(str(qty))
        except Exception:  # noqa: BLE001
            continue
        # 매수: 실제가 > 의도가 → 음수 슬리피지(불리)
        # 매도: 실제가 < 의도가 → 음수 슬리피지(불리)
        if side == "buy":
            total_slippage += (d_intended - d_actual) * d_qty
        else:
            total_slippage += (d_actual - d_intended) * d_qty

    # ─── 실현 P&L (Realized P&L) ──────────────
    realized = Decimal(0)
    by_strategy: dict[str, Decimal] = {}
    if _table_exists(positions_db, "realized_pnl"):
        if session_start_iso and session_end_iso:
            rp_rows = _safe_query(
                positions_db,
                """
                SELECT symbol, realized_pnl_krw, strategy_id, realized_at_utc
                FROM realized_pnl
                WHERE realized_at_utc >= ? AND realized_at_utc <= ?
                """,
                (session_start_iso, session_end_iso),
            )
        else:
            rp_rows = _safe_query(
                positions_db,
                """
                SELECT symbol, realized_pnl_krw, strategy_id, realized_at_utc
                FROM realized_pnl
                """,
            )
        for r in rp_rows:
            v = r.get("realized_pnl_krw")
            if v is None:
                continue
            try:
                d = Decimal(str(v))
            except Exception:  # noqa: BLE001
                continue
            realized += d
            sid = r.get("strategy_id") or "unknown"
            by_strategy[sid] = by_strategy.get(sid, Decimal(0)) + d

    # ─── 미실현 + 종목별 기여 (Unrealized + Symbol attribution) ──
    unrealized = Decimal(0)
    pos_market_value = Decimal(0)
    symbol_attr: list[dict[str, Any]] = []

    for pos in pos_rows:
        symbol = pos["symbol"]
        try:
            qty = Decimal(str(pos["quantity"]))
            avg_cost = Decimal(str(pos["avg_cost_krw"]))
        except Exception:  # noqa: BLE001
            continue

        mark = _get_mark_price(symbol, ohlcv_db, quote_db)
        if mark is None:
            mark = avg_cost

        mv = qty * mark
        upnl = qty * (mark - avg_cost)
        pos_market_value += mv
        unrealized += upnl

        cost_basis = qty * avg_cost
        upnl_pct = (upnl / cost_basis * Decimal(100)) if cost_basis != 0 else Decimal(0)

        symbol_attr.append({
            "symbol": symbol,
            "quantity": str(qty),
            "avg_cost_krw": str(avg_cost),
            "mark_price_krw": str(mark),
            "cost_basis_krw": str(cost_basis),
            "market_value_krw": str(mv),
            "unrealized_pnl_krw": str(upnl),
            "unrealized_pnl_pct": str(upnl_pct.quantize(Decimal("0.01"))),
        })

    total_equity = cash + pos_market_value
    total_pnl = realized + unrealized
    total_return_pct = (
        ((total_equity - starting) / starting * Decimal(100))
        if starting > 0 else Decimal(0)
    )

    # 전략 기여도 — 실현 P&L 기반
    strategy_attr = [
        {
            "strategy_id": sid,
            "realized_pnl_krw": str(amt),
        }
        for sid, amt in sorted(by_strategy.items(), key=lambda x: -float(x[1]))
    ]

    return {
        "starting_capital_krw": str(starting),
        "cash_krw": str(cash),
        "position_market_value_krw": str(pos_market_value),
        "total_equity_krw": str(total_equity),
        "realized_pnl_krw": str(realized),
        "unrealized_pnl_krw": str(unrealized),
        "total_pnl_krw": str(total_pnl),
        "total_return_pct": str(total_return_pct.quantize(Decimal("0.0001"))),
        "total_fees_krw": str(total_fees),
        "total_taxes_krw": str(total_taxes),
        "total_slippage_krw": str(total_slippage),
        "symbol_attribution": symbol_attr,
        "strategy_attribution": strategy_attr,
        "fill_count": len(fill_rows),
        "open_position_count": len(pos_rows),
    }
