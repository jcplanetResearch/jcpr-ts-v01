"""Streamlit 사이드바 default 값 자동 로더 (Phase 2 — Sidebar Automation).

3-tier fallback chain:
    1. 환경변수 (.env / shell)        ← 최우선
    2. capacity.local.yaml             ← 자본 정보 등 운영자 yaml
    3. project_root + 표준 경로 default ← 최종 fallback

설계 원칙 (Design Principles):
    - read-only — 어떤 환경 상태도 변경하지 않음
    - silent failure — yaml 미존재 / 파싱 실패 시 0 / 빈 문자열 fallback
    - 출처 추적 (sources mapping) — UI 가 "어디서 왔는가" 표시 가능
    - immutable — SidebarDefaults 는 frozen dataclass
    - sidebar UI 의 운영자 자유도 보존 — 사용자가 사이드바에서 직접 override 가능

보안 (Security):
    - 환경변수 값을 로깅하지 않음 (private 정보 가능성)
    - capacity.local.yaml 파싱 실패 시 silent (절대 yaml 내용을 예외 메시지에 포함하지 않음)
    - .env 파일은 dotenv 로드 시점 책임 — 본 모듈은 os.environ 만 read

후방 호환성 (Backward Compatibility):
    - app.py 는 기존 os.environ.get(...) 호출을 SidebarDefaults 필드 참조로 대체만 하면 됨
    - 사용자 sidebar 입력 동작 변경 없음 (값 default 만 변경)
    - 12개 필드 모두 일관된 fallback 메커니즘 적용
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

logger = logging.getLogger(__name__)


# 출처 식별자 (sources mapping 의 값)
SOURCE_ENV: str = "env"           # 환경변수에서 로드
SOURCE_YAML: str = "yaml"         # capacity.local.yaml 에서 로드
SOURCE_DEFAULT: str = "default"   # project_root 기반 default

# 표준 경로 default (project_root 기준)
_DEFAULT_POSITIONS_DB: str = "data/positions.sqlite"
_DEFAULT_OHLCV_DB: str = "data/ohlcv.sqlite"
_DEFAULT_QUOTE_DB: str = "data/quotes.sqlite"
_DEFAULT_RISK_AUDIT: str = "data/audit/risk_decisions.jsonl"
_DEFAULT_EXEC_AUDIT: str = "data/audit/executions.jsonl"
_DEFAULT_RECON_AUDIT: str = "data/audit/reconciliation.jsonl"
_DEFAULT_REJECTION_REPORT: str = "data/audit/rejections.jsonl"
_DEFAULT_KILL_SWITCH: str = "runtime/KILL_SWITCH_ON"
_DEFAULT_CAPACITY_YAML: str = "configs/capacity.local.yaml"


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SidebarDefaults:
    """Streamlit 사이드바 default 값 — 3-tier fallback 결과.

    모든 path 필드는 절대 경로 (project_root 기반 resolve 됨).
    사용자가 sidebar 에서 빈 문자열 또는 다른 값을 입력하면 그 값이 우선.

    Attributes:
        positions_db: 포지션 SQLite 경로
        ohlcv_db: OHLCV SQLite 경로
        quote_db: Quote SQLite 경로
        risk_audit_path: risk_decisions.jsonl 경로 (D1)
        execution_audit_path: executions.jsonl 경로 (D2)
        reconciliation_audit_path: reconciliation.jsonl 경로
        rejection_report_path: rejections.jsonl 경로
        kill_switch_file: KILL_SWITCH_ON 파일 경로
        capacity_config: capacity.local.yaml 경로
        starting_capital_krw: 시작 자본 (yaml 또는 0)
        cash_krw: 현재 현금 (yaml 또는 0)
        sources: 각 필드의 출처 ("env" / "yaml" / "default") 매핑
    """

    positions_db: str
    ohlcv_db: str
    quote_db: str
    risk_audit_path: str
    execution_audit_path: str
    reconciliation_audit_path: str
    rejection_report_path: str
    kill_switch_file: str
    capacity_config: str
    starting_capital_krw: float
    cash_krw: float
    sources: Mapping[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def resolve_sidebar_defaults(
    *,
    project_root: Path | None = None,
) -> SidebarDefaults:
    """3-tier fallback chain 으로 사이드바 default 값 산출.

    Args:
        project_root: 명시적 프로젝트 루트. None 이면 CWD 사용.

    Returns:
        SidebarDefaults — 12 필드 + sources 매핑.

    Notes:
        - 실패는 silent (graceful) — UI 가 빈 문자열 또는 0 표시.
        - 환경변수 값은 로깅하지 않음 (private 정보 가능성).
        - yaml 파싱 실패 시 sources 에 "default" 표시 + starting_capital=0.
    """
    if project_root is None:
        project_root = Path.cwd()
    project_root = project_root.resolve()

    sources: dict[str, str] = {}

    # --- DB 경로 (env > default) ---
    positions_db, src = _resolve_path_field(
        env_var="JCPR_POSITIONS_DB",
        default_relative=_DEFAULT_POSITIONS_DB,
        project_root=project_root,
    )
    sources["positions_db"] = src

    ohlcv_db, src = _resolve_path_field(
        env_var="JCPR_OHLCV_DB",
        default_relative=_DEFAULT_OHLCV_DB,
        project_root=project_root,
    )
    sources["ohlcv_db"] = src

    quote_db, src = _resolve_path_field(
        env_var="JCPR_QUOTE_DB",
        default_relative=_DEFAULT_QUOTE_DB,
        project_root=project_root,
    )
    sources["quote_db"] = src

    risk_audit_path, src = _resolve_path_field(
        env_var="JCPR_RISK_AUDIT",
        default_relative=_DEFAULT_RISK_AUDIT,
        project_root=project_root,
    )
    sources["risk_audit_path"] = src

    execution_audit_path, src = _resolve_path_field(
        env_var="JCPR_EXEC_AUDIT",
        default_relative=_DEFAULT_EXEC_AUDIT,
        project_root=project_root,
    )
    sources["execution_audit_path"] = src

    reconciliation_audit_path, src = _resolve_path_field(
        env_var="JCPR_RECON_AUDIT",
        default_relative=_DEFAULT_RECON_AUDIT,
        project_root=project_root,
    )
    sources["reconciliation_audit_path"] = src

    rejection_report_path, src = _resolve_path_field(
        env_var="JCPR_REJECTION_REPORT",
        default_relative=_DEFAULT_REJECTION_REPORT,
        project_root=project_root,
    )
    sources["rejection_report_path"] = src

    kill_switch_file, src = _resolve_path_field(
        env_var="JCPR_KILL_SWITCH",
        default_relative=_DEFAULT_KILL_SWITCH,
        project_root=project_root,
    )
    sources["kill_switch_file"] = src

    capacity_config, src = _resolve_path_field(
        env_var="JCPR_DASHBOARD_CAPACITY_YAML",
        default_relative=_DEFAULT_CAPACITY_YAML,
        project_root=project_root,
    )
    sources["capacity_config"] = src

    # --- 자본 정보 (yaml > 0) ---
    starting_capital_krw, cash_krw, capital_source = _load_capital_from_yaml(
        Path(capacity_config)
    )
    sources["starting_capital_krw"] = capital_source
    sources["cash_krw"] = capital_source

    return SidebarDefaults(
        positions_db=positions_db,
        ohlcv_db=ohlcv_db,
        quote_db=quote_db,
        risk_audit_path=risk_audit_path,
        execution_audit_path=execution_audit_path,
        reconciliation_audit_path=reconciliation_audit_path,
        rejection_report_path=rejection_report_path,
        kill_switch_file=kill_switch_file,
        capacity_config=capacity_config,
        starting_capital_krw=starting_capital_krw,
        cash_krw=cash_krw,
        sources=dict(sources),
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_path_field(
    *,
    env_var: str,
    default_relative: str,
    project_root: Path,
) -> tuple[str, str]:
    """단일 path 필드 resolve.

    Returns:
        (path_string, source_identifier).
        env_var 값이 빈 문자열이면 default 로 fallback.
    """
    env_value = os.environ.get(env_var, "").strip()
    if env_value:
        # 환경변수 값은 사용자 제공 — 그대로 사용 (절대/상대 모두 허용)
        # absolute path 변환은 사용자 정의에 맡김
        return env_value, SOURCE_ENV

    default_path = project_root / default_relative
    return str(default_path), SOURCE_DEFAULT


def _load_capital_from_yaml(
    yaml_path: Path,
) -> tuple[float, float, str]:
    """capacity.local.yaml 에서 starting_capital_krw + cash_krw 추출.

    silent failure — 어떤 오류도 raise 하지 않음.

    Returns:
        (starting_capital_krw, cash_krw, source).
        source 는 yaml 성공 시 "yaml", 실패/미존재 시 "default" (= 0).

    Notes:
        yaml 내용은 어떤 경우에도 로그 메시지에 포함되지 않음 (private 보호).
    """
    if not yaml_path.exists():
        return 0.0, 0.0, SOURCE_DEFAULT

    # yaml 파싱은 best-effort — yaml 모듈 import 실패 시에도 fallback
    try:
        import yaml  # local import (yaml 미설치 환경에도 module 자체는 import 가능)
    except ImportError:
        logger.warning(
            "PyYAML not installed; sidebar capital defaults will be 0"
        )
        return 0.0, 0.0, SOURCE_DEFAULT

    try:
        with yaml_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception:  # noqa: BLE001 — silent
        # yaml 파싱 실패 — 내용을 메시지에 포함하지 않기 위해 generic 로그만
        logger.warning(
            "capacity.local.yaml load failed (path=%s); using defaults",
            yaml_path,
        )
        return 0.0, 0.0, SOURCE_DEFAULT

    if not isinstance(data, dict):
        return 0.0, 0.0, SOURCE_DEFAULT

    # capital_caps 블록
    capital = data.get("capital_caps")
    if not isinstance(capital, dict):
        return 0.0, 0.0, SOURCE_DEFAULT

    # starting_capital_krw — total_deployed_capital.amount
    starting = 0.0
    total_dep = capital.get("total_deployed_capital")
    if isinstance(total_dep, dict):
        amount = total_dep.get("amount")
        starting = _safe_float(amount)

    # cash_krw — capital_caps.current_cash_krw (신규 키, 선택)
    cash_raw = capital.get("current_cash_krw")
    cash = _safe_float(cash_raw)

    # 둘 중 하나라도 0 이상이면 yaml source 인정
    if starting > 0 or cash > 0:
        return starting, cash, SOURCE_YAML

    return 0.0, 0.0, SOURCE_DEFAULT


def _safe_float(value) -> float:
    """안전한 float 변환. 실패 시 0.0."""
    if value is None:
        return 0.0
    try:
        result = float(value)
        if result < 0:
            # 음수는 운영 자본으로 의미 없음 — 0 으로 보정
            return 0.0
        return result
    except (TypeError, ValueError):
        return 0.0


__all__ = (
    "SOURCE_ENV",
    "SOURCE_YAML",
    "SOURCE_DEFAULT",
    "SidebarDefaults",
    "resolve_sidebar_defaults",
)
