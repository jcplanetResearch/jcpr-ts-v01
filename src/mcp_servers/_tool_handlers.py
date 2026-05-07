"""
도구 핸들러 (Tool Handlers)
=============================

JCPR Trading System - jcpr-ts-v01
Task 34 v0.1

8개 read-only 도구의 실제 구현. 기존 모듈을 wrapping.
(Implementations of 8 read-only tools — wraps existing modules.)

각 핸들러:
    - 입력 검증 (_security)
    - 데이터 소스 read-only 접근
    - 결과 마스킹 (mask_output)
    - 예외는 표준 형식으로 변환

핸들러 = MCP tool 래퍼와 분리된 순수 함수 (테스트 용이).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from ._config import ReadOnlyServerConfig
from ._security import (
    mask_output,
    validate_iso_datetime,
    validate_limit,
    validate_sector_map,
    validate_trace_id,
)


# ─────────────────────────────────────────────────
# 표준 에러 형식 (Standard Error Format)
# ─────────────────────────────────────────────────

def _error_response(code: str, message: str, **extra) -> dict[str, Any]:
    """표준 에러 응답."""
    return {
        "ok": False,
        "error_code": code,
        "error_message": message,
        **extra,
    }


def _ok_response(**data) -> dict[str, Any]:
    """표준 성공 응답."""
    return {
        "ok": True,
        "computed_at_utc": datetime.now(timezone.utc).isoformat(),
        **data,
    }


# ─────────────────────────────────────────────────
# Tool 1: get_market_status
# ─────────────────────────────────────────────────

def get_market_status(config: ReadOnlyServerConfig) -> dict[str, Any]:
    """
    시장 상태 조회.

    Task 11 calendar 의존이 본 세션에 없으므로,
    KRX 표준 규칙으로 대체 구현 (KST 09:00-15:30).
    Task 11이 추가되면 import 변경.
    """
    try:
        # KST = UTC+9
        now_utc = datetime.now(timezone.utc)
        # KST hour
        kst_hour = (now_utc.hour + 9) % 24
        kst_minute = now_utc.minute
        kst_weekday = now_utc.weekday()  # 0=월
        # KST 자정 보정
        if (now_utc.hour + 9) >= 24:
            kst_weekday = (kst_weekday + 1) % 7

        is_weekday = kst_weekday < 5
        in_hours = (
            (kst_hour > 9 or (kst_hour == 9 and kst_minute >= 0))
            and (kst_hour < 15 or (kst_hour == 15 and kst_minute <= 30))
        )

        if not is_weekday:
            state = "closed_weekend"
        elif kst_hour < 9:
            state = "pre_market"
        elif in_hours:
            state = "open"
        elif kst_hour == 15 and kst_minute > 30:
            state = "closed_post"
        elif kst_hour >= 16:
            state = "closed_post"
        else:
            state = "closed"

        return _ok_response(
            tool="get_market_status",
            state=state,
            market="KRX",
            timezone="Asia/Seoul",
            kst_time=f"{kst_hour:02d}:{kst_minute:02d}",
            is_trading_day=is_weekday,
            is_in_session=(state == "open"),
            note=(
                "Approximation — Task 11 calendar 미통합. "
                "한국 공휴일/조기 종료 미반영."
            ),
        )
    except Exception as e:  # noqa: BLE001
        return _error_response("MARKET_STATUS_ERROR", str(e))


# ─────────────────────────────────────────────────
# Tool 2: get_positions
# ─────────────────────────────────────────────────

def get_positions(config: ReadOnlyServerConfig) -> dict[str, Any]:
    """
    포지션 조회 (Task 25 positions DB).

    DB 미설정 시 빈 응답.
    """
    if not config.positions_db:
        return _ok_response(
            tool="get_positions",
            positions=[],
            count=0,
            note="positions_db 미설정 — JCPR_POSITIONS_DB 환경변수 설정",
        )

    db_path = Path(config.positions_db)
    if not db_path.exists():
        return _error_response(
            "DB_NOT_FOUND",
            f"positions DB not found: {config.positions_db}",
        )

    try:
        # mode=ro URI — 쓰기 차단
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            conn.row_factory = sqlite3.Row
            # 일반적 positions 테이블 가정 (Task 25 spec)
            # 실제 스키마와 다를 수 있으므로 동적 컬럼
            cur = conn.execute("""
                SELECT * FROM positions
                WHERE qty > 0
                ORDER BY symbol
            """)
            rows = [dict(r) for r in cur.fetchall()]
            return mask_output(_ok_response(
                tool="get_positions",
                positions=rows,
                count=len(rows),
            ))
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e).lower():
            return _ok_response(
                tool="get_positions",
                positions=[],
                count=0,
                note=f"positions 테이블 없음: {e}",
            )
        return _error_response("DB_ERROR", str(e))
    except Exception as e:  # noqa: BLE001
        return _error_response("POSITIONS_ERROR", str(e))


# ─────────────────────────────────────────────────
# Tool 3: get_pnl_snapshot
# ─────────────────────────────────────────────────

def get_pnl_snapshot(
    config: ReadOnlyServerConfig,
    *,
    starting_capital_krw: str,
    cash_krw: str,
) -> dict[str, Any]:
    """
    P&L 스냅샷 — Task 49 pnl_loader 사용 시도.

    Task 49가 본 컨테이너에 없을 수 있으므로 fallback 처리.
    """
    try:
        # Decimal 검증
        try:
            starting = Decimal(starting_capital_krw)
            cash = Decimal(cash_krw)
        except Exception as e:
            return _error_response(
                "INVALID_AMOUNT",
                f"starting_capital_krw/cash_krw must be valid decimal: {e}",
            )

        if starting <= 0:
            return _error_response(
                "INVALID_AMOUNT",
                "starting_capital_krw must be > 0",
            )

        # 포지션 시가 계산 (positions DB 사용)
        positions_resp = get_positions(config)
        if not positions_resp.get("ok"):
            return positions_resp

        positions = positions_resp.get("positions", [])
        position_value = Decimal(0)
        for p in positions:
            mv = p.get("market_value_krw") or p.get("market_value")
            if mv is not None:
                try:
                    position_value += Decimal(str(mv))
                except Exception:  # noqa: BLE001
                    continue

        equity = cash + position_value
        pnl = equity - starting
        pnl_pct = (pnl / starting) if starting > 0 else Decimal(0)

        return mask_output(_ok_response(
            tool="get_pnl_snapshot",
            starting_capital_krw=str(starting),
            cash_krw=str(cash),
            position_value_krw=str(position_value),
            equity_krw=str(equity),
            pnl_krw=str(pnl),
            pnl_pct=str(pnl_pct.quantize(Decimal("0.0001"))),
            position_count=len(positions),
        ))
    except Exception as e:  # noqa: BLE001
        return _error_response("PNL_ERROR", str(e))


# ─────────────────────────────────────────────────
# Tool 4: get_recent_fills
# ─────────────────────────────────────────────────

def get_recent_fills(
    config: ReadOnlyServerConfig,
    *,
    limit: Optional[int] = None,
    since_iso: Optional[str] = None,
) -> dict[str, Any]:
    """체결 조회 (Task 24 fills DB)."""
    try:
        limit = validate_limit(
            limit, default=50, max_value=config.max_fills_returned,
        )
        since = validate_iso_datetime(since_iso)

        # fills 테이블은 positions_db에 함께 있을 수도 있음 (Task 24 spec)
        if not config.positions_db:
            return _ok_response(
                tool="get_recent_fills",
                fills=[],
                count=0,
                note="DB 미설정",
            )

        db_path = Path(config.positions_db)
        if not db_path.exists():
            return _error_response("DB_NOT_FOUND", str(db_path))

        try:
            uri = f"file:{db_path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
            try:
                conn.row_factory = sqlite3.Row
                if since:
                    cur = conn.execute("""
                        SELECT * FROM fills
                        WHERE timestamp_utc >= ?
                        ORDER BY timestamp_utc DESC
                        LIMIT ?
                    """, (since, limit))
                else:
                    cur = conn.execute("""
                        SELECT * FROM fills
                        ORDER BY timestamp_utc DESC
                        LIMIT ?
                    """, (limit,))
                rows = [dict(r) for r in cur.fetchall()]
                return mask_output(_ok_response(
                    tool="get_recent_fills",
                    fills=rows,
                    count=len(rows),
                    limit_used=limit,
                ))
            finally:
                conn.close()
        except sqlite3.OperationalError as e:
            if "no such table" in str(e).lower():
                return _ok_response(
                    tool="get_recent_fills",
                    fills=[],
                    count=0,
                    note=f"fills 테이블 없음",
                )
            return _error_response("DB_ERROR", str(e))
    except ValueError as e:
        return _error_response("VALIDATION_ERROR", str(e))
    except Exception as e:  # noqa: BLE001
        return _error_response("FILLS_ERROR", str(e))


# ─────────────────────────────────────────────────
# Tool 5: get_rejection_summary
# ─────────────────────────────────────────────────

def get_rejection_summary(
    config: ReadOnlyServerConfig,
    *,
    since_iso: Optional[str] = None,
) -> dict[str, Any]:
    """
    거부 요약 (Task 19 audit log).

    JSONL 파일 스캔.
    """
    try:
        since = validate_iso_datetime(since_iso)
        since_dt = None
        if since:
            try:
                since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
                if since_dt.tzinfo is None:
                    since_dt = since_dt.replace(tzinfo=timezone.utc)
            except Exception:  # noqa: BLE001
                pass

        if not config.risk_audit_path:
            return _ok_response(
                tool="get_rejection_summary",
                total_decisions=0,
                rejections=0,
                approvals=0,
                by_reason={},
                by_gate={},
                note="risk_audit_path 미설정",
            )

        path = Path(config.risk_audit_path)
        if not path.exists():
            return _ok_response(
                tool="get_rejection_summary",
                total_decisions=0,
                rejections=0,
                approvals=0,
                by_reason={},
                by_gate={},
                note=f"파일 없음: {path}",
            )

        total = 0
        rejections = 0
        approvals = 0
        by_reason: dict[str, int] = {}
        by_gate: dict[str, int] = {}
        max_lines = 100_000  # 안전 한도

        with path.open("r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i >= max_lines:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                # 시간 필터
                if since_dt:
                    ts = obj.get("timestamp_utc") or obj.get("ts")
                    if ts:
                        try:
                            ev_dt = datetime.fromisoformat(
                                str(ts).replace("Z", "+00:00")
                            )
                            if ev_dt.tzinfo is None:
                                ev_dt = ev_dt.replace(tzinfo=timezone.utc)
                            if ev_dt < since_dt:
                                continue
                        except Exception:  # noqa: BLE001
                            pass
                total += 1
                # decision은 multiple field name 시도
                decision = (
                    obj.get("decision")
                    or obj.get("payload", {}).get("decision")
                    or ""
                )
                if isinstance(decision, str):
                    if decision.lower() in ("reject", "rejected", "deny", "denied"):
                        rejections += 1
                        reason = (
                            obj.get("reason")
                            or obj.get("payload", {}).get("reason")
                            or "unknown"
                        )
                        if isinstance(reason, str):
                            by_reason[reason] = by_reason.get(reason, 0) + 1
                        gate = (
                            obj.get("gate")
                            or obj.get("payload", {}).get("gate")
                            or "unknown"
                        )
                        if isinstance(gate, str):
                            by_gate[gate] = by_gate.get(gate, 0) + 1
                    elif decision.lower() in ("approve", "approved", "pass"):
                        approvals += 1

        return _ok_response(
            tool="get_rejection_summary",
            total_decisions=total,
            rejections=rejections,
            approvals=approvals,
            by_reason=by_reason,
            by_gate=by_gate,
            since_iso=since,
        )
    except ValueError as e:
        return _error_response("VALIDATION_ERROR", str(e))
    except Exception as e:  # noqa: BLE001
        return _error_response("REJECTION_ERROR", str(e))


# ─────────────────────────────────────────────────
# Tool 6: get_portfolio_risk
# ─────────────────────────────────────────────────

def get_portfolio_risk(
    config: ReadOnlyServerConfig,
    *,
    sector_map: Optional[dict] = None,
    cash_krw: str = "0",
) -> dict[str, Any]:
    """포트폴리오 리스크 분석 (Task 47 PortfolioRiskAnalyzer)."""
    try:
        smap = validate_sector_map(sector_map)

        try:
            cash = Decimal(cash_krw)
        except Exception as e:
            return _error_response("INVALID_AMOUNT", f"cash_krw: {e}")

        # positions
        positions_resp = get_positions(config)
        if not positions_resp.get("ok"):
            return positions_resp

        positions_list = positions_resp.get("positions", [])
        positions_dict: dict[str, dict[str, Any]] = {}
        position_value = Decimal(0)
        for p in positions_list:
            sym = p.get("symbol")
            mv = p.get("market_value_krw") or p.get("market_value") or 0
            if sym:
                try:
                    mv_d = Decimal(str(mv))
                except Exception:  # noqa: BLE001
                    continue
                positions_dict[str(sym)] = {"market_value_krw": mv_d}
                position_value += mv_d

        equity = cash + position_value

        # Task 47 사용
        try:
            from src.risk import PortfolioRiskAnalyzer, PortfolioRiskConfig
        except ImportError:
            return _error_response(
                "MODULE_MISSING",
                "src.risk (Task 47) 미통합 — Task 47을 먼저 빌드하세요.",
            )

        analyzer = PortfolioRiskAnalyzer(
            sector_map=smap,
            config=PortfolioRiskConfig(),
        )
        snap = analyzer.analyze(
            positions=positions_dict,
            equity_krw=equity,
        )

        return mask_output(_ok_response(
            tool="get_portfolio_risk",
            snapshot=snap.to_dict(),
        ))
    except ValueError as e:
        return _error_response("VALIDATION_ERROR", str(e))
    except Exception as e:  # noqa: BLE001
        return _error_response("RISK_ERROR", str(e))


# ─────────────────────────────────────────────────
# Tool 7: get_strategy_registry
# ─────────────────────────────────────────────────

def get_strategy_registry(config: ReadOnlyServerConfig) -> dict[str, Any]:
    """전략 레지스트리 조회 (Task 45)."""
    try:
        if not config.strategy_registry_path:
            return _ok_response(
                tool="get_strategy_registry",
                registry={},
                note="strategy_registry_path 미설정",
            )

        try:
            from src.strategies import load_registry
        except ImportError:
            return _error_response(
                "MODULE_MISSING",
                "src.strategies (Task 45) 미통합",
            )

        path = Path(config.strategy_registry_path)
        if not path.exists():
            return _error_response(
                "FILE_NOT_FOUND",
                f"strategy_registry not found: {path}",
            )

        registry = load_registry(str(path))
        return mask_output(_ok_response(
            tool="get_strategy_registry",
            registry=registry.summary(),
        ))
    except Exception as e:  # noqa: BLE001
        return _error_response("REGISTRY_ERROR", str(e))


# ─────────────────────────────────────────────────
# Tool 8: get_trace
# ─────────────────────────────────────────────────

def get_trace(
    config: ReadOnlyServerConfig,
    *,
    trace_id: str,
    include_tree: bool = True,
) -> dict[str, Any]:
    """trace_id의 전체 이벤트 + tree 조회 (Task A3)."""
    try:
        if not config.enable_get_trace:
            return _error_response(
                "DISABLED",
                "get_trace 도구가 설정에 의해 비활성화됨",
            )
        tid = validate_trace_id(trace_id)
        if not tid:
            return _error_response("VALIDATION_ERROR", "trace_id required")

        try:
            from src.observability import AuditIndexer
        except ImportError:
            return _error_response("MODULE_MISSING", "Task A3 미통합")

        indexer = AuditIndexer(audit_dir=config.audit_dir)
        events = indexer.find_by_trace(tid)
        if not events:
            return _ok_response(
                tool="get_trace",
                trace_id=tid,
                event_count=0,
                events=[],
                summary=None,
                tree=None,
                note="trace_id를 찾을 수 없음",
            )

        # 한도 적용
        max_n = config.max_trace_events_returned
        truncated = len(events) > max_n
        events_use = events[:max_n]

        summary = indexer.trace_summary(tid)
        tree = indexer.build_trace_tree(tid) if include_tree else None

        result: dict[str, Any] = {
            "tool": "get_trace",
            "trace_id": tid,
            "event_count": len(events),
            "returned_count": len(events_use),
            "truncated": truncated,
            "events": [e.to_dict() for e in events_use],
            "summary": summary.to_dict() if summary else None,
        }
        if tree:
            result["tree"] = tree.to_dict()

        return mask_output(_ok_response(**result))
    except ValueError as e:
        return _error_response("VALIDATION_ERROR", str(e))
    except Exception as e:  # noqa: BLE001
        return _error_response("TRACE_ERROR", str(e))


# ─────────────────────────────────────────────────
# Tool Registry (Inventory)
# ─────────────────────────────────────────────────

ALL_TOOLS = (
    "get_market_status",
    "get_positions",
    "get_pnl_snapshot",
    "get_recent_fills",
    "get_rejection_summary",
    "get_portfolio_risk",
    "get_strategy_registry",
    "get_trace",
)
