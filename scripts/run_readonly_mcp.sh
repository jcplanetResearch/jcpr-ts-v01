#!/bin/bash
# JCPR MCP Read-Only Server — stdio launcher
# Task 34 v0.1
#
# 환경변수 설정 후 stdio 모드로 MCP 서버 실행.
# Sets environment variables and launches stdio MCP server.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ─── 기본 환경변수 (필요시 수정) ─────────────
: "${JCPR_AUDIT_DIR:=${REPO_DIR}/data/audit}"
: "${JCPR_SESSION_ID:=mcp-$(date -u +%Y%m%d-%H%M%S)}"

# Optional: 데이터 경로
: "${JCPR_POSITIONS_DB:=${REPO_DIR}/data/positions.sqlite}"
: "${JCPR_STRATEGY_REGISTRY:=${REPO_DIR}/configs/strategy_registry.yaml}"
: "${JCPR_RISK_AUDIT:=${REPO_DIR}/data/risk_decisions.jsonl}"

export JCPR_AUDIT_DIR JCPR_SESSION_ID JCPR_POSITIONS_DB
export JCPR_STRATEGY_REGISTRY JCPR_RISK_AUDIT

# ─── 자격증명 의심 환경변수 사전 차단 ────────
forbidden_pattern='JCPR_.*\(PASSWORD\|SECRET\|TOKEN\|API_KEY\|AUTH\|CREDENTIAL\|PRIVATE_KEY\)'
if env | grep -qE "$forbidden_pattern"; then
    echo "❌ ERROR: 자격증명 의심 환경변수 발견 — MCP 서버는 자격증명을 절대 처리 안 함" >&2
    env | grep -E "$forbidden_pattern" | cut -d= -f1 >&2
    exit 1
fi

# ─── audit 디렉터리 생성 ────────────────────
mkdir -p "$JCPR_AUDIT_DIR"

echo "[jcpr-readonly] Launching stdio MCP server" >&2
echo "  audit_dir:   $JCPR_AUDIT_DIR" >&2
echo "  session_id:  $JCPR_SESSION_ID" >&2

# stdio 모드 — stdout은 JSON-RPC 전용, stderr만 로깅
exec python3 "${SCRIPT_DIR}/run_readonly_mcp.py"
