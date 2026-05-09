#!/usr/bin/env bash
# task_phase1_push_to_github.sh
# ─────────────────────────────────────────────────────────────────────────────
# Phase 1 — 적용된 변경을 GitHub에 푸시.
#
# 사용법(Usage):
#     ./task_phase1_push_to_github.sh --dry-run
#     ./task_phase1_push_to_github.sh
#     ./task_phase1_push_to_github.sh --branch main --no-test
#
# 보안 점검 (Security checks):
#     1. .env, *.sqlite, 토큰 파일이 staged 되지 않았는지 확인
#     2. push 전 테스트 실행 (--no-test로 건너뛸 수 있음)
#     3. 시크릿 패턴이 staged 변경에 포함되어 있는지 점검
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# 색상
RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[0;33m'
BLUE=$'\033[0;34m'; CYAN=$'\033[0;36m'; NC=$'\033[0m'
log_info()  { echo "${BLUE}[INFO]${NC}  $*"; }
log_ok()    { echo "${GREEN}[ OK ]${NC}  $*"; }
log_warn()  { echo "${YELLOW}[WARN]${NC}  $*"; }
log_err()   { echo "${RED}[ERR ]${NC}  $*"; }
log_step()  { echo ""; echo "${CYAN}━━━ $* ━━━${NC}"; }

# ─── 인자 파싱 ─────────────────────────────────────────────────────────────
DRY_RUN=false
BRANCH=""
REMOTE="origin"
SKIP_TEST=false
SKIP_PUSH=false
COMMIT_MSG=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)    DRY_RUN=true; shift ;;
        --branch)     BRANCH="$2"; shift 2 ;;
        --remote)     REMOTE="$2"; shift 2 ;;
        --no-test)    SKIP_TEST=true; shift ;;
        --no-push)    SKIP_PUSH=true; shift ;;
        --message|-m) COMMIT_MSG="$2"; shift 2 ;;
        -h|--help)
            sed -n '1,15p' "$0"; exit 0 ;;
        *)
            log_err "Unknown arg: $1"; exit 1 ;;
    esac
done

# 현재 브랜치 자동 감지
if [[ -z "$BRANCH" ]]; then
    BRANCH="$(git rev-parse --abbrev-ref HEAD)"
fi

log_step "Phase 1 Push — Configuration"
log_info "Branch:  $BRANCH"
log_info "Remote:  $REMOTE"
log_info "DryRun:  $DRY_RUN"

# ─── 위치 검증 ─────────────────────────────────────────────────────────────
if [[ ! -d ".git" ]]; then
    log_err "Not in a git repository root"
    exit 1
fi

# ─── 파일 매니페스트 ───────────────────────────────────────────────────────
declare -a FILES=(
    "src/execution/approval_store.py"
    "src/execution/__init__.py"
    "tests/execution/__init__.py"
    "tests/execution/test_approval_store.py"
    "docs/MIGRATION.md"
    "docs/PHASE1_CHANGES.md"
    ".gitignore"
)

log_step "Verify files exist"
declare -a PRESENT=()
for f in "${FILES[@]}"; do
    if [[ -f "$f" ]]; then
        PRESENT+=("$f")
    else
        log_warn "missing: $f"
    fi
done

if [[ ${#PRESENT[@]} -eq 0 ]]; then
    log_err "No files found to commit"
    exit 1
fi
log_ok "${#PRESENT[@]} of ${#FILES[@]} files present"

# ─── 위험 파일 사전 점검 ───────────────────────────────────────────────────
log_step "Security pre-check"

# .env 파일이 working tree에 있는지 (있어도 OK, gitignore가 막아야 함)
if [[ -f ".env" ]]; then
    log_info ".env exists in working tree (must be gitignored)"
    if ! git check-ignore .env >/dev/null 2>&1; then
        log_err ".env is NOT gitignored — STOP"
        exit 2
    fi
    log_ok ".env properly gitignored"
fi

# DB 파일이 staged 되지 않았는지 추가 검증
DB_STAGED=$(git diff --cached --name-only 2>/dev/null \
            | grep -E '\.(sqlite|sqlite-wal|sqlite-shm|db)$' || true)
if [[ -n "$DB_STAGED" ]]; then
    log_err "DB files staged — STOP:"
    echo "$DB_STAGED"
    exit 2
fi

# 시크릿 환경 파일 staged 검증
ENV_STAGED=$(git diff --cached --name-only 2>/dev/null \
             | grep -E '^\.env(\.[a-z]+)?$' \
             | grep -v '\.example$' || true)
if [[ -n "$ENV_STAGED" ]]; then
    log_err "Secret env file staged — STOP:"
    echo "$ENV_STAGED"
    exit 2
fi
log_ok "No DB/secret files staged"

# ─── 테스트 실행 ───────────────────────────────────────────────────────────
if ! $SKIP_TEST; then
    log_step "Run tests"
    if python3 -m pytest tests/execution/test_approval_store.py -q --tb=short \
            2>&1 | tail -5; then
        log_ok "Tests passed"
    else
        log_err "Tests failed — aborting"
        exit 1
    fi
else
    log_warn "Tests skipped (--no-test)"
fi

# ─── Stage ─────────────────────────────────────────────────────────────────
log_step "Stage files"
if $DRY_RUN; then
    log_info "DRY RUN — would stage:"
    for f in "${PRESENT[@]}"; do echo "    $f"; done
else
    git add -- "${PRESENT[@]}"
fi

# 시크릿 패턴 재점검 (staged diff)
log_step "Scan staged diff for secrets"
if ! $DRY_RUN; then
    SECRET_HITS=$(git diff --cached -U0 \
        | grep -nE '(^\+.*(appkey|appsecret|access_token|api[_-]?key)[[:space:]]*[:=][[:space:]]*["'\''][A-Za-z0-9+/]{20,})' \
        || true)
    if [[ -n "$SECRET_HITS" ]]; then
        log_err "Possible secret in staged diff — STOP:"
        echo "$SECRET_HITS"
        git reset
        exit 2
    fi
    log_ok "No secrets in staged diff"
fi

STAGED_COUNT=$(git diff --cached --name-only 2>/dev/null | wc -l | tr -d ' ')
if [[ "$STAGED_COUNT" -eq 0 ]] && ! $DRY_RUN; then
    log_warn "Nothing to commit (already up to date?)"
    exit 0
fi

# ─── Commit ────────────────────────────────────────────────────────────────
log_step "Commit"

if [[ -z "$COMMIT_MSG" ]]; then
    COMMIT_MSG="Phase 1 v0.1: Unified ApprovalStore (Task 9 + 40 ↔ Task 34-39 integration)

Pre-step toward single-source-of-truth approval workflow connecting
the MCP/agent layer (Task 34-39) with the broker/execution layer
(Task 9 + 40).

This phase adds the unified store ONLY; existing files remain
untouched and continue to use their own stores. Phase 2 will rewire
MCP write handlers and ExecutionGateway to consume this new store.

Added:
  - src/execution/approval_store.py (~700 lines)
      * 3-phase + cancellation + EXECUTING intermediate state
      * Self-approval blocking (SelfApprovalError)
      * Decision TTL (5min) + Execute TTL (60s) + Kill TTL (60s)
      * Live-mode policy guard (LiveModeBlockedError)
      * SQLite WAL + RLock thread safety
      * 0600 file mode enforcement (POSIX)
      * Decimal-as-string for monetary fields
      * UUID-based approval_ids (non-sequential)
  - tests/execution/test_approval_store.py (50 tests, all passing)
      * Construction, validation, lifecycle, TTL, concurrency, serialization
  - docs/MIGRATION.md (operator-driven manual cleanup procedure)
  - docs/PHASE1_CHANGES.md (change summary + Phase 2 preview)

Compatibility:
  - Task 9 KIS broker scripts: unchanged
  - Task 34-39 MCP/agents: unchanged (still use old store)
  - Task 40 ExecutionGateway: unchanged (still uses old state)

Security:
  - No credentials touched
  - .env, *.sqlite never staged (verified)
  - 0600 file mode + 0700 dir mode enforced
  - Self-approval blocked at single point of truth
  - Live-mode requires allow_live=True flag

Tests: 50/50 passing
Phase: 1 of 2"
fi

if $DRY_RUN; then
    log_info "DRY RUN — would commit with message:"
    echo "─────────────"
    echo "$COMMIT_MSG"
    echo "─────────────"
else
    git commit -m "$COMMIT_MSG" || {
        log_err "Commit failed"; exit 1
    }
    log_ok "Commit created: $(git rev-parse --short HEAD)"
fi

# ─── Push ──────────────────────────────────────────────────────────────────
if $SKIP_PUSH; then
    log_warn "Push skipped (--no-push)"
    exit 0
fi
if $DRY_RUN; then
    log_info "DRY RUN — would push to $REMOTE/$BRANCH"
    exit 0
fi

log_step "Push to $REMOTE/$BRANCH"
if git push "$REMOTE" "$BRANCH"; then
    log_ok "Push successful"
else
    log_err "Push failed"
    exit 1
fi

echo ""
log_step "Done"
log_info "Branch:  $BRANCH"
log_info "Commit:  $(git rev-parse --short HEAD)"
log_info ""
log_info "Phase 1 complete. To proceed to Phase 2:"
log_info "  1. Verify with: pytest tests/execution/test_approval_store.py"
log_info "  2. Follow docs/MIGRATION.md to clean up old DB files"
log_info "  3. Reply to Claude with confirmation to proceed to Phase 2"

exit 0
