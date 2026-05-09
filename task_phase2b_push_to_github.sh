#!/usr/bin/env bash
# task_phase2b_push_to_github.sh — JCPR-ts-v01 Phase 2-B
# ======================================================
#
# 목적: Phase 2-B 변경사항을 GitHub로 푸시.
#
# 사용법:
#   ./task_phase2b_push_to_github.sh --dry-run
#   ./task_phase2b_push_to_github.sh
#
# 안전 원칙 (safety invariants):
#   - 시크릿 커밋 차단: .env / *.key / tokens.json / credentials.json
#   - data/*.sqlite* 푸시 차단 (.gitignore 검증)
#   - 강제 푸시(force-push) 절대 금지
#   - 새 브랜치 또는 명시된 브랜치만 사용
#   - pytest 통과 강제 (--dry-run 제외)
#
set -euo pipefail

DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=true
fi

REPO_ROOT="${REPO_ROOT:-$(pwd)}"
BRANCH="${BRANCH:-phase2b-mcp-handler-integration}"
COMMIT_MSG="${COMMIT_MSG:-feat(phase2b): integrate MCP write handlers with ExecutionGateway}"

echo "================================================================"
echo "JCPR-ts-v01 Phase 2-B GitHub 푸시"
echo "================================================================"
echo "  REPO_ROOT = ${REPO_ROOT}"
echo "  BRANCH    = ${BRANCH}"
echo "  DRY_RUN   = ${DRY_RUN}"
echo "================================================================"

cd "${REPO_ROOT}"

# -------- 사전 검증 --------
if [[ ! -d ".git" ]]; then
    echo "ERROR: ${REPO_ROOT} is not a git repository" >&2
    exit 1
fi

# Phase 2-A 적용 확인
if [[ ! -f "src/execution/execution_gateway.py" ]]; then
    echo "ERROR: Phase 2-A ExecutionGateway 없음 — Phase 2-A 먼저 적용/푸시" >&2
    exit 1
fi
if [[ ! -f "src/mcp_servers/_write_handlers.py" ]]; then
    echo "ERROR: Phase 2-B _write_handlers.py 없음 — sync 스크립트 먼저 실행" >&2
    exit 1
fi

# -------- 시크릿 커밋 차단 --------
echo ""
echo "[1/8] 시크릿 커밋 차단 검사..."
STAGED=$(git diff --cached --name-only 2>/dev/null || true)
ALL_CHANGED=$(git status --porcelain 2>/dev/null | awk '{print $2}' || true)
COMBINED="${STAGED}
${ALL_CHANGED}"

for forbidden_pattern in '\.env$' '\.env\.[^e]' '\.key$' 'tokens\.json$' 'credentials\.json$' 'secrets\.ya?ml$'; do
    if echo "${COMBINED}" | grep -E "${forbidden_pattern}" > /dev/null 2>&1; then
        echo "FATAL: 시크릿 후보 파일 변경 감지: ${forbidden_pattern}" >&2
        echo "       작업 중단. 해당 파일을 .gitignore에 추가하세요." >&2
        exit 2
    fi
done
echo "  ✓ 시크릿 파일 변경 없음"

# -------- 폐기 모듈 잔존 확인 --------
echo ""
echo "[2/8] 폐기 모듈 잔존 확인..."
for legacy in src/mcp_servers/_approval_store.py src/execution/_approval_state.py; do
    if [[ -f "${legacy}" ]]; then
        echo "FATAL: 폐기 모듈 ${legacy} 가 아직 존재합니다." >&2
        echo "       sync 스크립트로 먼저 삭제하세요." >&2
        exit 3
    fi
done
echo "  ✓ 폐기 모듈 모두 삭제 확인"

# -------- 잔존 import 검사 --------
echo ""
echo "[3/8] 잔존 import 검사..."
leftover=$(grep -rn '_approval_store\|_approval_state\|_execute_action_stub' src/ tests/ scripts/ 2>/dev/null | grep -v '\.phase' || true)
if [[ -n "${leftover}" ]]; then
    echo "FATAL: 잔존 import 발견 — 푸시 차단" >&2
    echo "${leftover}" | sed 's/^/    /' >&2
    exit 4
fi
echo "  ✓ 잔존 import 없음"

# -------- data/*.sqlite* 푸시 차단 --------
echo ""
echo "[4/8] DB 파일 푸시 방지 검사..."
if echo "${COMBINED}" | grep -E 'data/.*\.sqlite' > /dev/null 2>&1; then
    echo "FATAL: data/*.sqlite 파일이 git에 추적되고 있습니다" >&2
    echo "       .gitignore에 다음 추가 필요:" >&2
    echo "         data/*.sqlite" >&2
    echo "         data/*.sqlite-shm" >&2
    echo "         data/*.sqlite-wal" >&2
    exit 5
fi
if [[ -f ".gitignore" ]]; then
    if ! grep -qE 'data/.*\.sqlite|data/approvals' .gitignore; then
        echo "  ⚠ .gitignore에 data/*.sqlite 패턴 없음 — 추가 권장"
    else
        echo "  ✓ .gitignore에 DB 패턴 포함 확인"
    fi
fi

# -------- pytest 통과 검증 --------
echo ""
echo "[5/8] 테스트 통과 사전 검증..."
if [[ "${DRY_RUN}" == "false" ]]; then
    if ! pytest tests/ -q 2>&1 | tail -3; then
        echo "FATAL: 테스트 실패 — 푸시 중단" >&2
        exit 6
    fi
    echo "  ✓ 테스트 통과 확인"
else
    echo "  [dry-run] pytest 실행 생략"
fi

# -------- 브랜치 준비 --------
echo ""
echo "[6/8] 브랜치 ${BRANCH} 준비..."
if [[ "${DRY_RUN}" == "false" ]]; then
    if git rev-parse --verify "${BRANCH}" >/dev/null 2>&1; then
        git checkout "${BRANCH}"
        echo "  ✓ 기존 브랜치 체크아웃"
    else
        git checkout -b "${BRANCH}"
        echo "  ✓ 신규 브랜치 생성"
    fi
else
    echo "  [dry-run] 브랜치 작업 생략"
fi

# -------- 스테이징 --------
echo ""
echo "[7/8] 변경 파일 스테이징..."
declare -a FILES_TO_ADD=(
    "src/mcp_servers/_write_handlers.py"
    "src/mcp_servers/restricted_server.py"
    "src/agents/prompts/tools/restricted_tools.md"
    "scripts/approve_cli.py"
    "tests/mcp_servers/test_write_handlers.py"
    "tests/mcp_servers/test_restricted_server.py"
    "tests/integration/test_phase2b_end_to_end.py"
    "tests/scripts/test_approve_cli.py"
    "tests/integration/__init__.py"
    "tests/scripts/__init__.py"
    "docs/PHASE2B_CHANGES.md"
)
for f in "${FILES_TO_ADD[@]}"; do
    if [[ -f "${f}" ]]; then
        if [[ "${DRY_RUN}" == "false" ]]; then
            git add "${f}"
            echo "  ✓ git add ${f}"
        else
            echo "  [dry-run] git add ${f}"
        fi
    fi
done

# 폐기 모듈 삭제 반영 (sync 단계에서 git rm 했다면 이미 staged)
for legacy in src/mcp_servers/_approval_store.py src/execution/_approval_state.py; do
    if git ls-files --error-unmatch "${legacy}" >/dev/null 2>&1; then
        if [[ "${DRY_RUN}" == "false" ]]; then
            git rm "${legacy}" 2>/dev/null || true
            echo "  ✓ git rm ${legacy}"
        else
            echo "  [dry-run] git rm ${legacy}"
        fi
    fi
done

# -------- 커밋 + 푸시 --------
echo ""
echo "[8/8] 커밋 & 푸시..."
if [[ "${DRY_RUN}" == "false" ]]; then
    if git diff --cached --quiet; then
        echo "  - 스테이징된 변경 없음 (skip commit)"
    else
        git commit -m "${COMMIT_MSG}" \
            -m "Phase 2-B: MCP write handlers + restricted_server + approve_cli unified, agent prompt refreshed." \
            -m "Tests added: 82 (write_handlers 32 + restricted_server 19 + e2e 18 + approve_cli 13)." \
            -m "Cumulative: Phase 1 (50) + Phase 2-A (43) + Phase 2-B (82) = 175 passing."
        echo "  ✓ 커밋 완료"
    fi
    # 강제 푸시 절대 금지 — 일반 push만
    git push origin "${BRANCH}"
    echo "  ✓ origin/${BRANCH} 푸시 완료"
    echo ""
    echo "다음 단계: GitHub에서 PR 생성 → 리뷰 → main 병합 → Phase 3 시작"
else
    echo "  [dry-run] 커밋 메시지: ${COMMIT_MSG}"
    echo "  [dry-run] git push origin ${BRANCH} (force-push 금지)"
fi

echo ""
echo "================================================================"
if [[ "${DRY_RUN}" == "true" ]]; then
    echo "[dry-run 완료] 실제 푸시는: ./task_phase2b_push_to_github.sh"
else
    echo "[푸시 완료] 브랜치: ${BRANCH}"
fi
echo "================================================================"
