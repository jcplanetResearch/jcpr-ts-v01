"""Phase 1 어댑터 ↔ Phase 2 view dict 변환기 (B2 — option B-full).

Phase 2 view 코드 패턴 (dict 반환 + error key) 보존을 위한 얇은
변환 레이어. view들은 기존처럼 dict를 받지만, 데이터 출처는 Phase 1
어댑터(CapacityConfig, PnLEngine, Reconciler, AuditLogReader)가 됨.

설계 원칙 (Design):
    - Phase 1 어댑터들이 사용 가능할 때 우선 사용
    - 어댑터 미가용시 graceful fallback (status="unavailable")
    - 모든 함수는 try/except로 감싸 dict로 결과 반환 (view 코드는
      try/except 없이 dict["error"] 또는 dict["status"]만 검사)
    - Phase 1 보안 게이트는 어댑터 내부에서 자동 호출됨 (이중 호출 X)

Output 항목 매핑:
    #6  strategy attribution → try_load_strategy_attribution
    #10 reconciliation       → try_load_reconciliation (manual trigger)
    #12 capacity recommendation → get_capacity_recommendation_status
    D5  starting_capital     → try_load_capacity_default
Phase 2 A2-1 PATCH for src/dashboard/views/_phase1_bridge.py.

이 파일은 기존 _phase1_bridge.py에 적용할 변경분만 담은 PATCH 문서이다.
실제 파일 교체가 아니며, 사용자가 기존 파일에 아래 변경을 머지(merge)해야 한다.

변경 요약 (Change Summary):
    1. import에 capacity_advisor / _session_history_reader 추가
    2. get_capacity_recommendation_status() 시그니처를 풍부화:
       - 기존: get_capacity_recommendation_status() -> dict
       - 신규: get_capacity_recommendation_status(*, config, session_summary,
                                                  reconciliation, audit_summary,
                                                  history_path=None) -> dict
    3. 반환 dict에 'recommendation' 키 추가 (CapacityRecommendation.to_dict() 형태).
       기존 호출자 호환을 위해 'available', 'reason' 키는 보존.

후방 호환성 (Backward Compatibility):
    - 인자 없이 호출하던 기존 코드는 default 시그니처로 호출되도록
      keyword-only 인자 + 모두 default 제공.
    - 인자가 모두 None / 부재 시 기존 placeholder 동작 유지
      (available=False, reason="manual").
    - overview_view.py 가 신규 'recommendation' 키를 읽어 새 카드 렌더.
"""
from __future__ import annotations
import logging
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

# 추가
# Phase 2 A2-1 신규 import (기존 import 블록 끝에 추가)
from src.risk.capacity_advisor import (
    CapacityAdvisor,
    HistoryStats,
    InvalidLadderError,
    SessionSignals,
)

from src.dashboard.views._session_history_reader import (
    HistoryReadResult,
    SessionHistoryError,
    try_load_history,
)

__all__ = [
    "try_load_capacity_default",
    "try_load_reconciliation",
    "try_load_strategy_attribution",
    "get_capacity_recommendation_status",
]

logger = logging.getLogger(__name__)

# =============================================================================
# B5: capacity.local.yaml 자동 default 로드
# =============================================================================

def try_load_capacity_default(
    capacity_yaml_path: Optional[str],
) -> Optional[dict[str, Any]]:
    """capacity.local.yaml 파싱 시도; 실패시 None.

    Phase 2 사이드바의 starting_capital default 갱신용 (D5 적용).
    운영자가 사이드바에서 다른 값을 입력하면 그것이 우선 — 본 함수는
    suggestion만 제공.

    Args:
        capacity_yaml_path: yaml 파일 경로 (사이드바 입력값).

    Returns:
        성공시 dict with keys:
            profile_name, operating_mode, currency,
            starting_capital_krw (float for st.number_input compatibility),
            daily_deployable_capital_krw, per_order_max_notional_krw,
            per_symbol_max_exposure_krw, last_modified,
            yaml_path
        실패시 None (caller가 fallback default 사용).
    """
    if not capacity_yaml_path:
        return None
    try:
        from src.dashboard._config import (
            DashboardConfigError,
            load_capacity_config,
        )
    except Exception as exc:
        logger.warning("Phase 1 _config import failed: %s", exc)
        return None

    try:
        cfg = load_capacity_config(Path(capacity_yaml_path))
    except DashboardConfigError as exc:
        logger.info("capacity.yaml load skipped (validation): %s", exc)
        return None
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("capacity.yaml unexpected error: %s", exc)
        return None

    return {
        "profile_name": cfg.profile_name,
        "operating_mode": cfg.operating_mode,
        "currency": cfg.currency,
        # st.number_input expects float; Decimal preserved internally
        "starting_capital_krw": float(cfg.starting_capital_krw),
        "daily_deployable_capital_krw": float(cfg.daily_deployable_capital_krw),
        "per_order_max_notional_krw": float(cfg.per_order_max_notional_krw),
        "per_symbol_max_exposure_krw": float(cfg.per_symbol_max_exposure_krw),
        "last_modified": cfg.last_modified,
        "yaml_path": str(cfg.yaml_path),
    }


# =============================================================================
# Output #10: Reconciliation (read from jsonl audit log)
# =============================================================================

def try_load_reconciliation(
    audit_path: Optional[str] = None,
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Reconciliation 결과를 jsonl audit log에서 읽음 (A3 — 옵션 (b)).

    Architecture:
        Dashboard never holds KIS credentials. A separate reconciler
        process (e.g., scripts/run_reconciler.py) runs with its own
        credential scope, writes ReconciliationReport.to_dict() rows to
        data/audit/reconciliation.jsonl, and the dashboard reads the
        latest row from there. This satisfies <requirement> "private
        information must not be leaked" — the dashboard process never
        sees the broker secrets.

    Args:
        audit_path: filesystem path to reconciliation.jsonl. None →
            'unavailable' status.
        force_refresh: ignored in v0.1.2; the reader does not cache
            (each call re-reads the file). Reserved for future caching.

    Returns:
        dict with keys:
            status: "available" | "unavailable" | "error"
            severity: "ok" | "minor" | "major" | None
            broker_position_count, ledger_position_count, mismatch_count
            mismatches: list of dicts (each from PositionMismatch.to_dict)
            captured_at_utc: iso string or None
            broker_cash_krw, broker_total_evaluation_krw: str (Decimal)
            all_matched: bool | None
            reason: explanatory text (status != "available")
    """
    _ = force_refresh  # placeholder for future caching policy
    if not audit_path:
        return {
            "status": "unavailable",
            "severity": None,
            "broker_position_count": None,
            "ledger_position_count": None,
            "mismatch_count": None,
            "mismatches": [],
            "captured_at_utc": None,
            "broker_cash_krw": None,
            "broker_total_evaluation_krw": None,
            "all_matched": None,
            "reason": (
                "reconciliation_audit_path 미설정. "
                "사이드바에서 경로 입력 또는 환경변수 JCPR_RECON_AUDIT 설정."
            ),
        }

    try:
        from src.dashboard.views._reconciliation_reader import (
            ReconciliationReader,
            ReconciliationReadError,
        )
    except ImportError as exc:  # pragma: no cover — defensive
        return {
            "status": "error",
            "severity": None,
            "broker_position_count": None,
            "ledger_position_count": None,
            "mismatch_count": None,
            "mismatches": [],
            "captured_at_utc": None,
            "broker_cash_krw": None,
            "broker_total_evaluation_krw": None,
            "all_matched": None,
            "reason": f"reconciliation_reader import failed: {exc}",
        }

    try:
        reader = ReconciliationReader(Path(audit_path))
        latest = reader.latest()
    except ReconciliationReadError as exc:
        return {
            "status": "error",
            "severity": None,
            "broker_position_count": None,
            "ledger_position_count": None,
            "mismatch_count": None,
            "mismatches": [],
            "captured_at_utc": None,
            "broker_cash_krw": None,
            "broker_total_evaluation_krw": None,
            "all_matched": None,
            "reason": str(exc),
        }

    if latest is None:
        return {
            "status": "unavailable",
            "severity": None,
            "broker_position_count": None,
            "ledger_position_count": None,
            "mismatch_count": None,
            "mismatches": [],
            "captured_at_utc": None,
            "broker_cash_krw": None,
            "broker_total_evaluation_krw": None,
            "all_matched": None,
            "reason": (
                "reconciliation.jsonl이 비어있거나 부재. "
                "별도 reconciler 프로세스 실행 필요 — "
                "예: python scripts/run_reconciler.py"
            ),
        }

    return {
        "status": "available",
        "severity": latest.get("severity"),
        "broker_position_count": latest.get("broker_position_count"),
        "ledger_position_count": latest.get("ledger_position_count"),
        "mismatch_count": latest.get("mismatch_count"),
        "mismatches": list(latest.get("mismatches") or []),
        "captured_at_utc": latest.get("captured_at_utc"),
        "broker_cash_krw": latest.get("broker_cash_krw"),
        "broker_total_evaluation_krw": latest.get("broker_total_evaluation_krw"),
        "all_matched": latest.get("all_matched"),
        "reason": None,
    }


# =============================================================================
# Output #6: Strategy attribution
# =============================================================================

def try_load_strategy_attribution(
    positions_db: Optional[str],
    ohlcv_db: Optional[str],
    quote_db: Optional[str],
    starting_capital_krw: float,
    cash_krw: float,
) -> list[dict[str, Any]]:
    """전략별 P&L 기여도를 가져옴.

    Phase 1 PnLEngine은 strategy_id별로 P&L을 분리하나, 그 사용은
    PnLEngine + PositionLedger + OHLCVStore + QuoteStore 인스턴스를
    요구함. Phase 2 v0.1.1의 data_loader는 이 인스턴스를 만들지
    않고 SQL을 직접 사용하므로, 본 함수는 다음 중 하나를 시도:

        1) Phase 1 PnLEngine을 사이드바 경로로 구성 (heavy, dependency 위험)
        2) data_loader.load_pnl_snapshot 결과에서 default_strategy_id로 그룹
        3) 빈 리스트 반환 + view에 "전략별 분류 미사용" 안내

    v0.1.1에서는 옵션 3 (graceful empty) — 이는 strategy registry
    (Task 45)가 별도로 구현된 후 통합 가능. 운영자에겐 "단일 전략
    가정 (momentum_v04)" 라벨 + total을 1개 strategy로 묶어 표시.

    Args:
        positions_db, ohlcv_db, quote_db, starting_capital_krw, cash_krw:
            data_loader.load_pnl_snapshot에 그대로 전달.

    Returns:
        list of dicts (each with strategy_id, realized_pnl_krw,
        unrealized_pnl_krw, fills_count, symbols). 빈 리스트는
        '데이터 없음' 의미.
    """
    if not positions_db:
        return []

    # data_loader의 기존 P&L 결과를 단일 strategy로 묶어 반환.
    # Task 45 (multi-strategy registry) 구현 후 strategy_id별 분리 가능.
    try:
        from src.dashboard.data_loader import load_pnl_snapshot
    except Exception as exc:
        logger.warning("data_loader import failed: %s", exc)
        return []

    snap = load_pnl_snapshot(
        positions_db, ohlcv_db, quote_db,
        starting_capital_krw, cash_krw,
    )
    if "error" in snap:
        return []

    sym_attr = snap.get("symbol_attribution", []) or []
    if not sym_attr:
        return []

    # 단일 strategy 가정 — Task 45 multi-strategy registry 구현 후 분리.
    symbols = [s["symbol"] for s in sym_attr]
    fills_count = len(symbols)  # proxy until fill-by-strategy lookup exists
    return [
        {
            "strategy_id": "momentum_v04",  # default per pnl_engine.py
            "realized_pnl_krw": float(snap.get("realized_pnl_krw", 0)),
            "unrealized_pnl_krw": float(snap.get("unrealized_pnl_krw", 0)),
            "fills_count": fills_count,
            "symbols": symbols,
        },
    ]


# =============================================================================
# Output #12: Capacity recommendation (Phase 1 placeholder bridge)
# =============================================================================

# ---------------------------------------------------------------------------
# PATCH: get_capacity_recommendation_status — 갱신된 함수 본문
# ---------------------------------------------------------------------------
# 기존 함수를 다음으로 교체:


def get_capacity_recommendation_status(
    *,
    config: Any = None,                       # CapacityConfig (사용자 로컬 정의)
    session_summary: Mapping[str, Any] | None = None,
    reconciliation: Mapping[str, Any] | None = None,
    audit_summary: Mapping[str, Any] | None = None,
    history_path: Path | None = None,
    enforce_history_permissions: bool = True,
) -> dict[str, Any]:
    """`<output>` #12: 다음 세션 capacity 권장 상태.

    Phase 2 A2-1: capacity_advisor 호출 + history reader 통합.

    Args:
        config: CapacityConfig 인스턴스. None 이면 manual 모드로 fallback.
        session_summary: 단일 세션 P&L 요약 dict.
            keys: realized_pnl_krw, unrealized_pnl_krw
        reconciliation: reconciliation 결과 dict (Phase 2 A3 산출물).
            keys: severity ("ok"/"minor"/"major") OR None (= "missing")
        audit_summary: audit 통계 dict.
            keys: exec_failed_count
        history_path: data/audit/sessions.jsonl 경로 (옵션, A2-2에서 활성).
        enforce_history_permissions: history jsonl 0600 강제 여부.

    Returns:
        dict with keys:
            - available: bool
            - reason: str
            - recommendation: dict | None  (CapacityRecommendation.to_dict())

        후방 호환성을 위해 호출자는 항상 'available'와 'reason'을 안전하게 읽을 수 있다.

    Notes:
        실패는 fail-open. 어떤 예외도 dashboard 전체를 죽이지 않으며
        available=False + reason 으로 graceful degradation 한다.
    """
    # 1. config 미제공 → manual fallback
    if config is None:
        return {
            "available": False,
            "reason": "manual",
            "recommendation": None,
            # Phase 2 B-full 호환 키
            "implemented": False,
            "recommended_amount_krw": None,
            "pending_module": "capacity_advisor (manual mode)",
        }

    # 2. ladder 미정의 → no_ladder
    ladder = getattr(config, "ladder", ()) or ()
    if not ladder:
        return {
            "available": False,
            "reason": "no_ladder",
            "recommendation": None,
            "implemented": False,
            "recommended_amount_krw": None,
            "pending_module": "capacity_advisor (no_ladder)",
        }

    # 3. CapacityAdvisor 인스턴스화 (ladder 검증)
    try:
        advisor = CapacityAdvisor(ladder=tuple(ladder))
    except InvalidLadderError as exc:
        logger.warning("capacity ladder 검증 실패: %s", exc)
        return {
            "available": False,
            "reason": "no_ladder",
            "recommendation": None,
            "implemented": False,
            "recommended_amount_krw": None,
            "pending_module": "capacity_advisor (no_ladder)",
        }

    # 4. SessionSignals 조립 (안전한 dict.get + Decimal 변환)
    try:
        session_signals = _build_session_signals(
            session_summary=session_summary or {},
            reconciliation=reconciliation,
            audit_summary=audit_summary or {},
            starting_capital_krw=getattr(config, "starting_capital_krw"),
        )
    except (ValueError, TypeError, AttributeError) as exc:
        logger.warning("SessionSignals 조립 실패: %s", exc)
        return {
            "available": False,
            "reason": "manual",
            "recommendation": None,
            "implemented": False,
            "recommended_amount_krw": None,
            "pending_module": "capacity_advisor (manual mode)",
        }

    # 5. history 시도 (fail-open)
    history: HistoryStats | None = None
    if history_path is not None:
        try:
            history_result = try_load_history(
                history_path,
                days=30,
                enforce_permissions=enforce_history_permissions,
            )
            if history_result is not None:
                history = HistoryStats(
                    sessions_count=history_result.sessions_count,
                    cumulative_realized_pnl_krw=history_result.cumulative_realized_pnl_krw,
                    max_drawdown_krw=history_result.max_drawdown_krw,
                    consecutive_loss_days=history_result.consecutive_loss_days,
                )
        except SessionHistoryError as exc:
            # 권한 위반 등 — 권장은 history 없이 진행 (보수적)
            logger.warning("session history 읽기 실패: %s", exc)
            history = None

    # 6. 권장 산출
    try:
        rec = advisor.recommend(session_signals, history=history)
    except Exception as exc:  # noqa: BLE001 — 마지막 안전망
        logger.exception("capacity_advisor.recommend 실패: %s", exc)
        return {
            "available": False,
            "reason": "manual",
            "recommendation": None,
            "implemented": False,
            "recommended_amount_krw": None,
            "pending_module": "capacity_advisor (manual mode)",
        }

    rec_dict = rec.to_dict()
    return {
        "available": rec.available,
        "reason": rec.reason,
        "recommendation": rec_dict,
        # Phase 2 B-full 호환 키
        "implemented": rec.available,
        "recommended_amount_krw": rec_dict.get("recommended_capacity_krw"),
        "pending_module": "capacity_advisor (active)",
    }

# ---------------------------------------------------------------------------
# Phase 2 A2-1 helpers: 20260510 16:57
# ---------------------------------------------------------------------------

def _build_session_signals(
    *,
    session_summary,
    reconciliation,
    audit_summary,
    starting_capital_krw,
):
    """대시보드 dict를 SessionSignals로 변환 (Phase 2 A2-1)."""
    from src.risk.capacity_advisor import SessionSignals
    from decimal import Decimal

    realized = _to_decimal(session_summary.get("realized_pnl_krw", 0))
    unrealized = _to_decimal(session_summary.get("unrealized_pnl_krw", 0))

    if not isinstance(starting_capital_krw, Decimal):
        starting_capital_krw = _to_decimal(starting_capital_krw)

    severity = _severity_from_recon(reconciliation)
    exception_count = int(audit_summary.get("exec_failed_count", 0) or 0)

    return SessionSignals(
        realized_pnl_krw=realized,
        unrealized_pnl_krw=unrealized,
        starting_capital_krw=starting_capital_krw,
        reconciliation_severity=severity,
        exception_count=exception_count,
    )


def _severity_from_recon(reconciliation):
    """reconciliation dict 에서 severity 추출 (Phase 2 A2-1)."""
    if reconciliation is None:
        return "missing"
    if not reconciliation.get("available", True):
        return "missing"
    severity = reconciliation.get("severity")
    if severity in ("ok", "minor", "major"):
        return severity
    return "missing"


def _to_decimal(value):
    """안전한 Decimal 변환 (Phase 2 A2-1)."""
    from decimal import Decimal

    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise ValueError(f"bool not allowed for Decimal conversion: {value!r}")
    if value is None:
        return Decimal("0")
    return Decimal(str(value))
