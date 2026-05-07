#!/bin/bash
# JCPR MCP Restricted Server — stdio launcher
# Task 35 v0.1

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# 기본 환경변수
: "${JCPR_AUDIT_DIR:=${REPO_DIR}/data/audit}"
: "${JCPR_APPROVAL_DB:=${REPO_DIR}/data/approvals.sqlite}"
: "${JCPR_SESSION_ID:=restricted-$(date -u +%Y%m%d-%H%M%S)}"
: "${JCPR_OPERATOR_ID:=operator-default}"
: "${JCPR_ALLOW_LIVE:=0}"

export JCPR_AUDIT_DIR JCPR_APPROVAL_DB JCPR_SESSION_ID JCPR_OPERATOR_ID JCPR_ALLOW_LIVE

# 자격증명 의심 환경변수 차단
forbidden='JCPR_.*\(PASSWORD\|SECRET\|TOKEN\|API_KEY\|AUTH\|CREDENTIAL\|PRIVATE_KEY\)'
if env | grep -qE "$forbidden"; then
    echo "❌ ERROR: 자격증명 의심 환경변수 — MCP는 자격증명을 처리하지 않음" >&2
    env | grep -E "$forbidden" | cut -d= -f1 >&2
    exit 1
fi

mkdir -p "$JCPR_AUDIT_DIR" "$(dirname "$JCPR_APPROVAL_DB")"

echo "[jcpr-restricted] Launching stdio MCP server" >&2
echo "  audit_dir:    $JCPR_AUDIT_DIR" >&2
echo "  approval_db:  $JCPR_APPROVAL_DB" >&2
echo "  operator_id:  $JCPR_OPERATOR_ID" >&2
echo "  allow_live:   $JCPR_ALLOW_LIVE" >&2

exec python3 "${SCRIPT_DIR}/run_restricted_mcp.py"
