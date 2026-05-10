"""
데이터 로더 (Data Loader)
==========================

JCPR Trading System - jcpr-ts-v01
Task 48 v0.1.1

DB / audit log / runtime 파일에서 대시보드용 데이터 로드.
(Loads dashboard data from DB / audit logs / runtime files.)

설계 원칙 (Design Principles):
    - 모든 함수는 순수 — Streamlit 의존 없음 (pure functions, no Streamlit dependency)
    - 캐싱은 호출 측(view)에서 @st.cache_data로 적용
    - DB 경로는 인자로 — 절대 하드코딩 금지 (no hardcoding)
    - 예외 발생 시 dict에 'error' 키로 반환 (graceful error)
    - 시크릿 절대 반환·로그 안 함 (never return/log secrets)
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd


# ─────────────────────────────────────────────────
# 데이터 소스 설정 (Data Source Config)
# ─────────────────────────────────────────────────

@dataclass(frozen=True)
class DashboardDataSource:
    """
    대시보드 데이터 소스 경로 모음 (immutable).

    필드 (Fields):
        positions_db: 포지션·체결·주문 DB (Task 23-25)
        ohlcv_db:     OHLCV 가격 DB (Task 12)
        quote_db:     실시간 호가 DB (Task 13)
        risk_audit_path: 리스크 게이트 감사 로그 (Task 19-20, JSONL)
        execution_audit_path: 실행 게이트웨이 감사 로그 (Task 21, JSONL)
        rejection_report_path: 최신 거부 분석 리포트 (Task 20)
        kill_switch_file: kill switch 파일 (Task 31)
        capacity_config: capacity.yaml 경로 (Task 5)
        reconciliation_audit_path: Reconciler 감사 로그 (Task 28, JSONL).
            A3 (옵션 Y): 별도 reconciler 프로세스가 작성한 jsonl을
            read-only로 읽음. 시크릿은 dashboard 외부에 격리됨.
    """
    positions_db: Optional[str] = None
    ohlcv_db: Optional[str] = None
    quote_db: Optional[str] = None
    risk_audit_path: Optional[str] = None
    execution_audit_path: Optional[str] = None
    rejection_report_path: Optional[str] = None
    kill_switch_file: Optional[str] = None
    capacity_config: Optional[str] = None
    reconciliation_audit_path: Optional[str] = None  # A3: 신규 필드


# ─────────────────────────────────────────────────
# SQLite 헬퍼 (SQLite Helper)
# ─────────────────────────────────────────────────

def _safe_query(db_path: str, query: str, params: tuple = ()) -> pd.DataFrame:
    """
    SQLite 안전 쿼리 — 파일 없거나 테이블 없으면 빈 DataFrame.

    (Safe SQLite query — returns empty DataFrame if file/table missing.)

    A1 (옵션 Y): layer 15 권한 검증을 sqlite3 연결 직전에 호출.
    위반 시 (0644 등) 빈 DataFrame 반환 — fail-closed. 사이드바에서
    이미 운영자에게 경고가 표시되므로 추가 안내 불필요.
    """
    if not db_path or not Path(db_path).exists():
        return pd.DataFrame()

    # A1: layer 15 권한 검증 (verify_db_permissions은 graceful — file
    # absent / non-POSIX 환경에서는 통과). DashboardSecurityError가
    # raise되면 권한 위반이므로 빈 DataFrame 반환.
    try:
        from src.dashboard._security import (
            verify_db_permissions,
            DashboardSecurityError,
        )
        verify_db_permissions(Path(db_path))
    except DashboardSecurityError:
        return pd.DataFrame()
    except ImportError:  # pragma: no cover — defensive
        # _security 모듈 부재 시 graceful — 검증 없이 통과
        pass

    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            return pd.read_sql_query(query, conn, params=params)
    except (sqlite3.Error, pd.errors.DatabaseError):
        return pd.DataFrame()


def _table_exists(db_path: str, table: str) -> bool:
    """테이블 존재 여부 체크."""
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


# ─────────────────────────────────────────────────
# JSONL 로더 (JSONL Loader)
# ─────────────────────────────────────────────────

def _read_jsonl_to_df(
    path: Path,
    *,
    since_utc: Optional[datetime] = None,
    time_field: str = "timestamp_utc",
    max_lines: int = 50_000,
) -> pd.DataFrame:
    """
    JSONL 파일을 DataFrame으로 — since_utc 이후 레코드만.
    """
    if not path.exists():
        return pd.DataFrame()

    records: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i >= max_lines:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if since_utc and time_field in rec:
                    try:
                        ts = datetime.fromisoformat(rec[time_field].replace("Z", "+00:00"))
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                        if ts < since_utc:
                            continue
                    except (ValueError, AttributeError):
                        pass
                records.append(rec)
    except OSError:
        return pd.DataFrame()

    if not records:
        return pd.DataFrame()
    return pd.DataFrame(records)


# ─────────────────────────────────────────────────
# 포지션 로더 (Positions Loader)
# ─────────────────────────────────────────────────

def load_positions(positions_db: Optional[str]) -> pd.DataFrame:
    """
    현재 보유 포지션 로드 (Load current open positions).

    Returns:
        DataFrame: symbol, quantity, avg_cost_krw, side, opened_at_utc
    """
    if not positions_db or not _table_exists(positions_db, "positions"):
        return pd.DataFrame()

    df = _safe_query(
        positions_db,
        """
        SELECT symbol, quantity, avg_cost_krw, side, opened_at_utc
        FROM positions
        WHERE quantity != 0
        ORDER BY symbol
        """,
    )
    return df


# ─────────────────────────────────────────────────
# 체결 로더 (Fills Loader)
# ─────────────────────────────────────────────────

def load_fills(
    positions_db: Optional[str],
    *,
    since_utc_iso: Optional[str] = None,
    limit: int = 500,
) -> pd.DataFrame:
    """
    체결 이력 로드 (Load fills history).

    Returns:
        DataFrame: fill_id, symbol, side, quantity, price_krw,
                   gross_krw, fee_krw, tax_krw, filled_at_utc
    """
    if not positions_db or not _table_exists(positions_db, "fills"):
        return pd.DataFrame()

    if since_utc_iso:
        df = _safe_query(
            positions_db,
            """
            SELECT fill_id, symbol, side, quantity, price_krw,
                   gross_krw, fee_krw, tax_krw, filled_at_utc
            FROM fills
            WHERE filled_at_utc >= ?
            ORDER BY filled_at_utc DESC
            LIMIT ?
            """,
            (since_utc_iso, limit),
        )
    else:
        df = _safe_query(
            positions_db,
            """
            SELECT fill_id, symbol, side, quantity, price_krw,
                   gross_krw, fee_krw, tax_krw, filled_at_utc
            FROM fills
            ORDER BY filled_at_utc DESC
            LIMIT ?
            """,
            (limit,),
        )
    return df


# ─────────────────────────────────────────────────
# P&L 스냅샷 로더 (P&L Snapshot Loader)
# ─────────────────────────────────────────────────

def load_pnl_snapshot(
    positions_db: Optional[str],
    ohlcv_db: Optional[str],
    quote_db: Optional[str],
    starting_capital_krw: float,
    cash_krw: float,
) -> dict[str, Any]:
    """
    P&L 스냅샷 계산 (Final Output #1-7 일부).

    Returns:
        dict with keys:
            starting_capital_krw, cash_krw,
            position_market_value_krw, total_equity_krw,
            realized_pnl_krw, unrealized_pnl_krw,
            total_pnl_krw, total_return_pct,
            total_fees_krw, total_taxes_krw,
            symbol_attribution (list of dict)
    """
    try:
        positions = load_positions(positions_db)
        fills = load_fills(positions_db)

        # 체결 누적 수수료·세금
        total_fees_krw = float(fills["fee_krw"].sum()) if "fee_krw" in fills.columns else 0.0
        total_taxes_krw = float(fills["tax_krw"].sum()) if "tax_krw" in fills.columns else 0.0

        # 실현 P&L: matched_pnl 테이블이 있으면 사용 (Task 26)
        realized_pnl_krw = 0.0
        if positions_db and _table_exists(positions_db, "realized_pnl"):
            df = _safe_query(
                positions_db,
                "SELECT SUM(realized_pnl_krw) AS total FROM realized_pnl",
            )
            if not df.empty and df.iloc[0]["total"] is not None:
                realized_pnl_krw = float(df.iloc[0]["total"])

        # 미실현 P&L: 현재 포지션 × (현재가 - 평단가)
        unrealized_pnl_krw = 0.0
        position_market_value_krw = 0.0
        symbol_attribution: list[dict[str, Any]] = []

        for _, pos in positions.iterrows():
            symbol = pos["symbol"]
            qty = float(pos["quantity"])
            avg_cost = float(pos["avg_cost_krw"])
            mark_price = _get_mark_price(symbol, ohlcv_db, quote_db)
            if mark_price is None:
                mark_price = avg_cost  # fallback

            mv = qty * mark_price
            upnl = qty * (mark_price - avg_cost)
            position_market_value_krw += mv
            unrealized_pnl_krw += upnl

            symbol_attribution.append({
                "symbol": symbol,
                "quantity": qty,
                "avg_cost_krw": avg_cost,
                "mark_price_krw": mark_price,
                "market_value_krw": mv,
                "unrealized_pnl_krw": upnl,
                "unrealized_pnl_pct": (upnl / (qty * avg_cost) * 100) if (qty * avg_cost) else 0.0,
            })

        total_equity_krw = cash_krw + position_market_value_krw
        total_pnl_krw = realized_pnl_krw + unrealized_pnl_krw
        total_return_pct = (
            (total_equity_krw - starting_capital_krw) / starting_capital_krw * 100
            if starting_capital_krw > 0 else 0.0
        )

        return {
            "starting_capital_krw": starting_capital_krw,
            "cash_krw": cash_krw,
            "position_market_value_krw": position_market_value_krw,
            "total_equity_krw": total_equity_krw,
            "realized_pnl_krw": realized_pnl_krw,
            "unrealized_pnl_krw": unrealized_pnl_krw,
            "total_pnl_krw": total_pnl_krw,
            "total_return_pct": total_return_pct,
            "total_fees_krw": total_fees_krw,
            "total_taxes_krw": total_taxes_krw,
            "symbol_attribution": symbol_attribution,
            "computed_at_utc": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


def _get_mark_price(
    symbol: str,
    ohlcv_db: Optional[str],
    quote_db: Optional[str],
) -> Optional[float]:
    """
    심볼의 mark price — quote 우선, OHLCV 종가 fallback.
    """
    # 1. 실시간 호가
    if quote_db and _table_exists(quote_db, "quotes"):
        df = _safe_query(
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
        if not df.empty and df.iloc[0]["mid"] is not None:
            return float(df.iloc[0]["mid"])

    # 2. OHLCV 최신 종가
    if ohlcv_db and _table_exists(ohlcv_db, "ohlcv_daily"):
        df = _safe_query(
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
        if not df.empty and df.iloc[0]["close_krw"] is not None:
            return float(df.iloc[0]["close_krw"])

    return None


# ─────────────────────────────────────────────────
# 거부 분석 로더 (Rejection Analysis Loader)
# ─────────────────────────────────────────────────

def load_rejection_summary(
    risk_audit_path: Optional[str],
    *,
    since_utc_iso: Optional[str] = None,
) -> dict[str, Any]:
    """
    리스크 거부 요약 (Task 20 형식).

    Returns:
        dict with keys:
            summary: total_evaluations, reject_count, rejection_rate,
                     by_gate (dict), by_reason (dict)
            diagnostic_findings: list of {severity, message, ...}
            window_30min_trend: list of {window_start, count, rate}
    """
    if not risk_audit_path:
        return {"error": "risk_audit_path not configured"}

    try:
        path = Path(risk_audit_path)
        since = (
            datetime.fromisoformat(since_utc_iso.replace("Z", "+00:00"))
            if since_utc_iso else None
        )
        df = _read_jsonl_to_df(path, since_utc=since, time_field="evaluated_at_utc")

        if df.empty:
            return {
                "summary": {
                    "total_evaluations": 0,
                    "reject_count": 0,
                    "rejection_rate": 0.0,
                    "by_gate": {},
                    "by_reason": {},
                },
                "diagnostic_findings": [],
                "window_30min_trend": [],
            }

        total = len(df)
        rejected = df[df.get("decision", "") == "reject"] if "decision" in df.columns else pd.DataFrame()
        reject_count = len(rejected)
        rate = reject_count / total if total > 0 else 0.0

        by_gate: dict[str, int] = {}
        by_reason: dict[str, int] = {}
        if not rejected.empty:
            if "rejected_gate" in rejected.columns:
                by_gate = rejected["rejected_gate"].value_counts().to_dict()
            if "rejection_reason" in rejected.columns:
                by_reason = rejected["rejection_reason"].value_counts().to_dict()

        # 진단 (간단 버전 — Task 20에서 상세)
        # 임계 (Thresholds): >=50% critical / >=20% warning
        findings: list[dict[str, Any]] = []
        if rate >= 0.5:
            findings.append({
                "severity": "critical",
                "message": f"거부율 {rate:.1%} — 즉시 점검 필요 (Rejection rate too high)",
            })
        elif rate >= 0.2:
            findings.append({
                "severity": "warning",
                "message": f"거부율 {rate:.1%} — 모니터링 권장 (Elevated rejection rate)",
            })

        # 30분 윈도우 추세
        window_trend: list[dict[str, Any]] = []
        if not df.empty and "evaluated_at_utc" in df.columns:
            try:
                df = df.copy()
                df["ts"] = pd.to_datetime(df["evaluated_at_utc"], utc=True, errors="coerce")
                df = df.dropna(subset=["ts"])
                if not df.empty:
                    df["window"] = df["ts"].dt.floor("30min")
                    grouped = df.groupby("window").agg(
                        total=("decision", "count"),
                        rejects=("decision", lambda x: (x == "reject").sum()),
                    ).reset_index()
                    grouped["rate"] = grouped["rejects"] / grouped["total"]
                    window_trend = [
                        {
                            "window_start": row["window"].isoformat(),
                            "count": int(row["total"]),
                            "rejects": int(row["rejects"]),
                            "rate": float(row["rate"]),
                        }
                        for _, row in grouped.iterrows()
                    ]
            except Exception:  # noqa: BLE001
                pass

        return {
            "summary": {
                "total_evaluations": int(total),
                "reject_count": int(reject_count),
                "rejection_rate": float(rate),
                "by_gate": by_gate,
                "by_reason": by_reason,
            },
            "diagnostic_findings": findings,
            "window_30min_trend": window_trend,
        }
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


# ─────────────────────────────────────────────────
# 시장 상태 로더 (Market Status Loader)
# ─────────────────────────────────────────────────

def load_market_status(now_utc: Optional[datetime] = None) -> dict[str, Any]:
    """
    KRX 시장 상태 — 단순 시간대 기반 추정.
    (Task 11의 calendar 모듈이 있으면 그걸 호출하는 것이 정확.
     여기는 fallback only.)

    Returns:
        dict: state, is_open, now_kst
    """
    try:
        now = now_utc or datetime.now(timezone.utc)
        # KST = UTC+9
        kst_hour = (now.hour + 9) % 24
        kst_minute = now.minute
        weekday = (now.weekday())  # 0=Mon
        # 주말
        if weekday >= 5:
            state = "closed_weekend"
            is_open = False
        # 09:00–15:30 KST 간단 판정 (장중)
        elif (kst_hour > 9 or (kst_hour == 9 and kst_minute >= 0)) and (
            kst_hour < 15 or (kst_hour == 15 and kst_minute < 30)
        ):
            state = "regular"
            is_open = True
        elif kst_hour < 9:
            state = "pre_market"
            is_open = False
        else:
            state = "after_hours"
            is_open = False

        # KST 시각 표기
        from datetime import timedelta
        kst = now + timedelta(hours=9)
        kst_str = kst.strftime("%Y-%m-%d %H:%M:%S KST")

        return {
            "state": state,
            "is_open": is_open,
            "now_utc": now.isoformat(),
            "now_kst": kst_str,
        }
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


# ─────────────────────────────────────────────────
# 감사 로그 요약 (Audit Summary)
# ─────────────────────────────────────────────────

def load_audit_summary(
    execution_audit_path: Optional[str],
    *,
    since_utc_iso: Optional[str] = None,
    limit: int = 200,
) -> pd.DataFrame:
    """
    실행 게이트웨이 감사 로그 (Task 21) — 최근 N건.
    """
    if not execution_audit_path:
        return pd.DataFrame()

    since = (
        datetime.fromisoformat(since_utc_iso.replace("Z", "+00:00"))
        if since_utc_iso else None
    )
    df = _read_jsonl_to_df(
        Path(execution_audit_path),
        since_utc=since,
        time_field="started_at_utc",
    )
    if df.empty:
        return df
    if "started_at_utc" in df.columns:
        df = df.sort_values("started_at_utc", ascending=False)
    return df.head(limit)


def load_kill_switch_status(kill_switch_file: Optional[str]) -> bool:
    """Kill switch 파일 존재 여부 — 캐시 안 함."""
    if not kill_switch_file:
        return False
    return Path(kill_switch_file).exists()
