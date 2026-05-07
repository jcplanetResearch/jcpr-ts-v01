#!/usr/bin/env bash
# ─────────────────────────────────────────────────
# JCPR Trading Dashboard 실행 스크립트
# (JCPR Trading Dashboard Run Script)
#
# Task 48 v0.1.1
#
# 보안 (Security):
#   - 127.0.0.1 바인딩만 허용 (localhost only)
#   - 외부 노출 금지 (no external exposure)
#   - 사용량 통계 비활성 (telemetry disabled)
# ─────────────────────────────────────────────────
set -euo pipefail

# 스크립트 위치 → repo root 추론
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
APP_PATH="${REPO_ROOT}/src/dashboard/app.py"

if [[ ! -f "${APP_PATH}" ]]; then
    echo "❌ Error: app.py not found at ${APP_PATH}" >&2
    exit 1
fi

# 기본 포트
PORT="${JCPR_DASHBOARD_PORT:-8501}"

echo "─────────────────────────────────────────"
echo "JCPR Trading Dashboard"
echo "  Repo:    ${REPO_ROOT}"
echo "  App:     ${APP_PATH}"
echo "  URL:     http://127.0.0.1:${PORT}"
echo "  ⚠️  로컬 전용 (Local only)"
echo "─────────────────────────────────────────"

cd "${REPO_ROOT}"

# 환경 변수 (선택적 — 사용자가 미리 export 가능)
#   export JCPR_POSITIONS_DB=...
#   export JCPR_OHLCV_DB=...
#   export JCPR_RISK_AUDIT=...

exec streamlit run "${APP_PATH}" \
    --server.address=127.0.0.1 \
    --server.port="${PORT}" \
    --server.headless=true \
    --browser.gatherUsageStats=false \
    --server.fileWatcherType=auto
