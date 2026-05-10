"""
탭 5: 시스템 (System)
======================

JCPR Trading System - jcpr-ts-v01
Task 48 v0.1.1

시장 상태 + Kill switch + 최근 예외 + 데이터 소스 상태.
Final Output #10 (reconciliation), #11 (exceptions).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

from ..components import build_market_status_text
from ..data_loader import (
    DashboardDataSource,
    load_audit_summary,
    load_kill_switch_status,
    load_market_status,
)
# B3 (옵션 B-full):
#   - Reconciliation 섹션 (Final Output #10) via Phase 1 bridge
#   - audit dataframe scrub_secrets 적용
from .._security import scrub_secrets
from ._phase1_bridge import try_load_reconciliation


def render_system_view(
    *,
    data_source: DashboardDataSource,
    since_utc_iso: str | None,
) -> None:
    """시스템 탭 렌더링."""
    st.header("시스템 (System)")

    # ─── 시장 상태 ─────────────────────────────
    st.subheader("시장 상태 (Market Status)")
    status = load_market_status()
    text = build_market_status_text(status)
    if status.get("is_open"):
        st.success(text)
    else:
        st.warning(text)

    st.divider()

    # ─── Kill Switch 상태 ──────────────────────
    st.subheader("Kill Switch 상태 (Kill Switch Status)")
    kill_active = load_kill_switch_status(data_source.kill_switch_file)
    if kill_active:
        st.error(
            f"🚨 Kill Switch ACTIVE — 모든 신규 거래 차단 (All new trades blocked)\n"
            f"파일 (File): `{data_source.kill_switch_file}`"
        )
    else:
        st.success(
            f"✅ Kill Switch INACTIVE — 정상 운영 (Normal operation)\n"
            f"감시 파일 (Watch file): `{data_source.kill_switch_file or 'not configured'}`"
        )

    st.divider()

    # ─── 데이터 소스 상태 ──────────────────────
    st.subheader("데이터 소스 상태 (Data Source Status)")
    sources = [
        ("Positions DB", data_source.positions_db),
        ("OHLCV DB", data_source.ohlcv_db),
        ("Quote DB", data_source.quote_db),
        ("Risk Audit Log", data_source.risk_audit_path),
        ("Execution Audit Log", data_source.execution_audit_path),
        ("Rejection Report", data_source.rejection_report_path),
        ("Capacity Config", data_source.capacity_config),
    ]
    rows = []
    for name, path in sources:
        if not path:
            status_str = "⚪ 미설정 (Not configured)"
            size = "—"
        else:
            p = Path(path)
            if p.exists():
                status_str = "✅ 존재 (Exists)"
                size = f"{p.stat().st_size:,} bytes"
            else:
                status_str = "❌ 없음 (Missing)"
                size = "—"
        rows.append({
            "소스 (Source)": name,
            "경로 (Path)": path or "—",
            "상태 (Status)": status_str,
            "크기 (Size)": size,
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    st.divider()

    # ─── 정합성 점검 (Reconciliation) — Final Output #10 ──────
    # B3 (옵션 B-full) + A3 (옵션 Y): Phase 1 bridge 경유 + jsonl reader.
    # 별도 reconciler 프로세스가 작성한 audit log를 read-only로 표시.
    # KIS API 호출은 dashboard 외부에서 수행 (시크릿 격리).
    st.subheader("정합성 점검 (Reconciliation) — Final Output #10")
    recon = try_load_reconciliation(
        audit_path=data_source.reconciliation_audit_path,
    )
    status = recon.get("status", "unavailable")

    if status == "available":
        severity = recon.get("severity", "ok")
        broker_n = recon.get("broker_position_count", 0)
        ledger_n = recon.get("ledger_position_count", 0)
        mismatch_n = recon.get("mismatch_count", 0)
        if severity == "ok":
            st.success(
                f"✅ 모두 일치 (All matched) — "
                f"broker={broker_n}, ledger={ledger_n}"
            )
        elif severity == "minor":
            st.warning(
                f"🟡 평균가 차이 {mismatch_n}건 (minor avg-price drift) — "
                f"broker={broker_n}, ledger={ledger_n}"
            )
        else:  # major
            st.error(
                f"🔴 수량/누락 불일치 {mismatch_n}건 (major mismatches) — "
                f"즉시 점검 필요 (immediate review required)"
            )
        # A3: broker cash + total evaluation 표시 (자산 규모 — localhost-only)
        cash_str = recon.get("broker_cash_krw")
        eval_str = recon.get("broker_total_evaluation_krw")
        if cash_str is not None or eval_str is not None:
            cols = st.columns(2)
            with cols[0]:
                st.metric("Broker 현금 (Cash)", cash_str or "—")
            with cols[1]:
                st.metric("Broker 총평가 (Total Eval)", eval_str or "—")
        if recon.get("mismatches"):
            st.dataframe(
                pd.DataFrame(recon["mismatches"]),
                use_container_width=True, hide_index=True,
            )
        ts = recon.get("captured_at_utc")
        if ts:
            st.caption(f"캡처 시각 (Captured): {ts}")
    elif status == "error":
        st.error(f"Reconciliation 오류: {recon.get('reason', '?')}")
    else:  # unavailable
        st.info(
            "ℹ️ Reconciliation 미실행 또는 audit log 부재 — "
            "별도 reconciler 프로세스 실행 필요."
        )
        if recon.get("reason"):
            st.caption(scrub_secrets(recon["reason"]))
        # A3: 외부 명령 안내 (시크릿은 dashboard 외부에서 처리됨을 명시)
        st.code(
            "# 별도 reconciler 프로세스 실행:\n"
            "# (KIS API 자격증명은 dashboard 외부 .env에서만 로드)\n"
            "python scripts/run_reconciler.py \\\n"
            "    --audit data/audit/reconciliation.jsonl",
            language="bash",
        )

    st.divider()

    # ─── 최근 실행 감사 (Recent Execution Audit) ──
    st.subheader("최근 실행 감사 (Recent Execution Audit) — Final Output #11")
    if data_source.execution_audit_path:
        audit_df = load_audit_summary(
            data_source.execution_audit_path,
            since_utc_iso=since_utc_iso,
            limit=100,
        )
        if audit_df.empty:
            st.info("감사 기록 없음 (No audit records).")
        else:
            # 시크릿 가능성이 있는 컬럼은 표시 안 함
            safe_cols = [
                c for c in audit_df.columns
                if not any(s in c.lower() for s in ["secret", "token", "key", "password", "auth"])
            ]
            # B3 (옵션 B-full): 컬럼 이름 필터에 더해 값 자체에도
            # scrub_secrets 적용 (broker error message 등에 시크릿이
            # 포함될 가능성 차단). 객체(dtype=object)로 한정 — 숫자/시각은
            # 빠르게 통과.
            safe_df = audit_df[safe_cols].head(50).copy()
            for col in safe_df.select_dtypes(include="object").columns:
                safe_df[col] = safe_df[col].astype(str).map(scrub_secrets)
            st.dataframe(
                safe_df,
                use_container_width=True,
                hide_index=True,
            )
            st.caption(
                f"총 {len(audit_df)}건 (시크릿성 컬럼 자동 숨김 + "
                f"값 단위 scrub 적용 / cols hidden + values scrubbed)"
            )
    else:
        st.info("execution_audit_path 미설정 (Not configured).")

    st.divider()

    # ─── 갱신 시각 ─────────────────────────────
    st.caption(
        f"마지막 갱신 (Last refresh): "
        f"{datetime.now(timezone.utc).isoformat()} UTC"
    )
