#!/usr/bin/env bash
# task_phase2b_sync_to_local.sh — JCPR-ts-v01 Phase 2-B
# =====================================================
#
# 목적: Phase 2-B 산출물을 로컬 jcpr-ts-v01/ 레포에 적용.
#
# 사용법:
#   ./task_phase2b_sync_to_local.sh --dry-run
#   ./task_phase2b_sync_to_local.sh
#
# 안전 원칙 (safety invariants):
#   - 시크릿(.env, *.key, tokens.json) 절대 건드리지 않음
#   - data/approvals.sqlite 절대 삭제하지 않음 (운영 데이터 보존)
#   - 기존 파일은 백업 후 교체
#   - _approval_store.py / _approval_state.py만 삭제 (Phase 2-A에서 위임된 작업)
#
set -euo pipefail

DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=true
fi

REPO_ROOT="${REPO_ROOT:-$(pwd)}"
SOURCE_DIR="${SOURCE_DIR:-/mnt/user-data/outputs/phase2b}"
BACKUP_DIR="${BACKUP_DIR:-${REPO_ROOT}/.phase2b_backup_$(date -u +%Y%m%dT%H%M%SZ)}"

echo "================================================================"
echo "JCPR-ts-v01 Phase 2-B 동기화 (sync to local)"
echo "================================================================"
echo "  REPO_ROOT  = ${REPO_ROOT}"
echo "  SOURCE_DIR = ${SOURCE_DIR}"
echo "  BACKUP_DIR = ${BACKUP_DIR}"
echo "  DRY_RUN    = ${DRY_RUN}"
echo "================================================================"

# -------- 사전 검증 --------
if [[ ! -d "${REPO_ROOT}/src" ]]; then
    echo "ERROR: ${REPO_ROOT}/src 디렉터리 없음" >&2
    exit 1
fi
if [[ ! -d "${SOURCE_DIR}" ]]; then
    echo "ERROR: SOURCE_DIR(${SOURCE_DIR}) 없음" >&2
    exit 1
fi
if [[ ! -f "${REPO_ROOT}/src/execution/approval_store.py" ]]; then
    echo "ERROR: Phase 1 ApprovalStore가 없음 — Phase 1 먼저 적용 필요" >&2
    exit 1
fi
if [[ ! -f "${REPO_ROOT}/src/execution/execution_gateway.py" ]]; then
    echo "ERROR: Phase 2-A ExecutionGateway가 없음 — Phase 2-A 먼저 적용 필요" >&2
    exit 1
fi

# -------- 시크릿 보호 검사 --------
echo ""
echo "[1/7] 시크릿 보호 사전 검사..."
for forbidden in .env credentials.json tokens.json; do
    if find "${SOURCE_DIR}" -name "${forbidden}" 2>/dev/null | grep -q .; then
        echo "FATAL: SOURCE_DIR에 ${forbidden} 발견 — 작업 중단" >&2
        exit 2
    fi
done
echo "  ✓ 시크릿 파일 없음 확인"

# -------- 백업 --------
echo ""
echo "[2/7] 기존 파일 백업..."
if [[ "${DRY_RUN}" == "false" ]]; then
    mkdir -p "${BACKUP_DIR}"
    for f in src/mcp_servers/_write_handlers.py \
             src/mcp_servers/restricted_server.py \
             src/mcp_servers/_approval_store.py \
             src/execution/_approval_state.py \
             scripts/approve_cli.py \
             src/agents/prompts/tools/restricted_tools.md; do
        if [[ -f "${REPO_ROOT}/${f}" ]]; then
            mkdir -p "${BACKUP_DIR}/$(dirname "${f}")"
            cp -p "${REPO_ROOT}/${f}" "${BACKUP_DIR}/${f}"
            echo "  ✓ 백업: ${f}"
        fi
    done
else
    echo "  [dry-run] 백업 디렉터리: ${BACKUP_DIR}"
fi

# -------- 신규/수정 파일 복사 --------
echo ""
echo "[3/7] Phase 2-B 파일 적용..."
declare -a TO_COPY=(
    "src/mcp_servers/_write_handlers.py"
    "src/mcp_servers/restricted_server.py"
    "src/agents/prompts/tools/restricted_tools.md"
    "scripts/approve_cli.py"
    "tests/mcp_servers/test_write_handlers.py"
    "tests/mcp_servers/test_restricted_server.py"
    "tests/integration/test_phase2b_end_to_end.py"
    "tests/scripts/test_approve_cli.py"
    "docs/PHASE2B_CHANGES.md"
)
for f in "${TO_COPY[@]}"; do
    if [[ "${DRY_RUN}" == "true" ]]; then
        echo "  [dry-run] 복사 예정: ${SOURCE_DIR}/${f} → ${REPO_ROOT}/${f}"
    else
        mkdir -p "${REPO_ROOT}/$(dirname "${f}")"
        cp -p "${SOURCE_DIR}/${f}" "${REPO_ROOT}/${f}"
        echo "  ✓ 적용: ${f}"
    fi
done

# 테스트 디렉터리 __init__.py 보장
if [[ "${DRY_RUN}" == "false" ]]; then
    for d in tests/integration tests/scripts; do
        mkdir -p "${REPO_ROOT}/${d}"
        touch "${REPO_ROOT}/${d}/__init__.py"
    done
fi

# -------- 잔존 모듈 삭제 --------
echo ""
echo "[4/7] 폐기된 모듈 삭제..."
for legacy in src/mcp_servers/_approval_store.py src/execution/_approval_state.py; do
    if [[ -f "${REPO_ROOT}/${legacy}" ]]; then
        if [[ "${DRY_RUN}" == "true" ]]; then
            echo "  [dry-run] 삭제 예정: ${legacy}"
        else
            if (cd "${REPO_ROOT}" && git ls-files --error-unmatch "${legacy}" >/dev/null 2>&1); then
                (cd "${REPO_ROOT}" && git rm "${legacy}")
                echo "  ✓ git rm ${legacy}"
            else
                rm "${REPO_ROOT}/${legacy}"
                echo "  ✓ rm ${legacy}"
            fi
        fi
    else
        echo "  - ${legacy} 이미 없음 (skip)"
    fi
done

# -------- 잔존 import 검사 --------
echo ""
echo "[5/7] 잔존 import 검사..."
if [[ "${DRY_RUN}" == "false" ]]; then
    leftover=$(cd "${REPO_ROOT}" && grep -rn '_approval_store\|_approval_state\|_execute_action_stub' src/ tests/ scripts/ 2>/dev/null || true)
    # 백업 디렉터리는 제외
    leftover=$(echo "${leftover}" | grep -v '\.phase' || true)
    if [[ -n "${leftover}" ]]; then
        echo "  ⚠ 잔존 import 발견:"
        echo "${leftover}" | sed 's/^/    /'
        echo "  → 운영자 수동 정리 필요 (operator must clean up)"
    else
        echo "  ✓ 잔존 import 없음"
    fi
fi

# -------- DB 권한 확인 --------
echo ""
echo "[6/7] DB 파일 권한 확인..."
if [[ -f "${REPO_ROOT}/data/approvals.sqlite" ]]; then
    PERM=$(stat -c '%a' "${REPO_ROOT}/data/approvals.sqlite" 2>/dev/null || stat -f '%Lp' "${REPO_ROOT}/data/approvals.sqlite")
    if [[ "${PERM}" != "600" ]]; then
        echo "  ⚠ data/approvals.sqlite 권한 ${PERM} — 0600으로 변경"
        if [[ "${DRY_RUN}" == "false" ]]; then
            chmod 600 "${REPO_ROOT}/data/approvals.sqlite"
            echo "  ✓ chmod 600 완료"
        fi
    else
        echo "  ✓ data/approvals.sqlite 권한 0600"
    fi
else
    echo "  - data/approvals.sqlite 없음 (첫 실행 시 자동 생성됨)"
fi

# -------- 운영자 점검 안내 --------
echo ""
echo "[7/7] 운영자 수동 확인 사항..."
echo "  1. 테스트 실행 (전체 누적):"
echo "       pytest tests/ -v"
echo "       기대: Phase 1 (50) + Phase 2-A (43) + Phase 2-B (82) = 175/175 PASSED"
echo "  2. CLI 동작 확인 (paper):"
echo "       python -m scripts.approve_cli list"
echo "       python -m scripts.approve_cli --help"
echo "  3. 잔존 import 0건 확인 (위 검사 결과 참조)"
echo "  4. .env에 다음 환경변수 정리 (필요 시):"
echo "       JCPR_APPROVAL_DB     # 기본값 data/approvals.sqlite"
echo "       JCPR_OPERATOR        # 운영자 이름 (CLI에서 --operator 대신)"
echo "       JCPR_MODE            # paper (기본) | live"
echo "       JCPR_ALLOW_LIVE      # live 모드 시 1 설정"

echo ""
echo "================================================================"
if [[ "${DRY_RUN}" == "true" ]]; then
    echo "[dry-run 완료] 실제 적용은: ./task_phase2b_sync_to_local.sh"
else
    echo "[적용 완료] 백업: ${BACKUP_DIR}"
fi
echo "================================================================"
