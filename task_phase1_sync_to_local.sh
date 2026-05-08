#!/usr/bin/env bash
# task_phase1_sync_to_local.sh
# ─────────────────────────────────────────────────────────────────────────────
# Phase 1 — ApprovalStore 통합 산출물을 로컬 jcpr-ts-v01 레포지토리에 적용.
#
# 사용법(Usage):
#     ./task_phase1_sync_to_local.sh --dry-run   # 미리보기
#     ./task_phase1_sync_to_local.sh             # 실제 적용
#     ./task_phase1_sync_to_local.sh --target /path/to/jcpr-ts-v01
#
# 보안 원칙(Security):
#     1. 이 스크립트는 시크릿을 절대 다루지 않음
#     2. 기존 DB 파일은 삭제하지 않음 (운영자 수동 처리)
#     3. .env 파일은 건드리지 않음
#     4. 작업 전 git working tree clean 여부 경고
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
TARGET_DIR=""
SKIP_GIT_CHECK=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)         DRY_RUN=true; shift ;;
        --target)          TARGET_DIR="$2"; shift 2 ;;
        --skip-git-check)  SKIP_GIT_CHECK=true; shift ;;
        -h|--help)
            sed -n '1,18p' "$0"
            exit 0
            ;;
        *)
            log_err "Unknown arg: $1"
            exit 1
            ;;
    esac
done

# ─── 경로 결정 ─────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="$SCRIPT_DIR"

if [[ -z "$TARGET_DIR" ]]; then
    # 기본: 부모 디렉터리에 jcpr-ts-v01이 있다고 가정
    GUESS="$(cd "$SCRIPT_DIR/.." && pwd)/jcpr-ts-v01"
    if [[ -d "$GUESS" ]]; then
        TARGET_DIR="$GUESS"
    else
        log_err "Cannot auto-detect target. Use --target /path/to/jcpr-ts-v01"
        exit 1
    fi
fi

log_step "Phase 1 Sync — Configuration"
log_info "Source:  $SOURCE_DIR"
log_info "Target:  $TARGET_DIR"
log_info "DryRun:  $DRY_RUN"

# ─── 사전 검증 ─────────────────────────────────────────────────────────────
if [[ ! -d "$TARGET_DIR" ]]; then
    log_err "Target directory does not exist: $TARGET_DIR"
    exit 1
fi
if [[ ! -d "$TARGET_DIR/.git" ]]; then
    log_err "Target is not a git repository: $TARGET_DIR"
    exit 1
fi

# Git working tree 검증
if ! $SKIP_GIT_CHECK; then
    cd "$TARGET_DIR"
    if [[ -n "$(git status --porcelain)" ]]; then
        log_warn "Target git working tree has uncommitted changes:"
        git status --short | head -10
        echo ""
        if ! $DRY_RUN; then
            read -r -p "Continue anyway? [y/N] " yn
            case "$yn" in
                [Yy]*) ;;
                *) log_info "Aborted by user."; exit 0 ;;
            esac
        fi
    fi
    cd "$SCRIPT_DIR"
fi

# ─── 파일 매니페스트 ───────────────────────────────────────────────────────
declare -a FILES=(
    "src/execution/approval_store.py"
    "src/execution/__init__.py"
    "tests/execution/__init__.py"
    "tests/execution/test_approval_store.py"
    "docs/MIGRATION.md"
    "docs/PHASE1_CHANGES.md"
)

# 검증: 모든 source 파일 존재
log_step "Verify source files"
for f in "${FILES[@]}"; do
    if [[ ! -f "$SOURCE_DIR/$f" ]]; then
        log_err "Missing source: $SOURCE_DIR/$f"
        exit 1
    fi
done
log_ok "All ${#FILES[@]} source files present"

# ─── 충돌 사전 점검 ────────────────────────────────────────────────────────
log_step "Check for existing files in target"
declare -a EXISTING=()
for f in "${FILES[@]}"; do
    if [[ -f "$TARGET_DIR/$f" ]]; then
        EXISTING+=("$f")
    fi
done

if [[ ${#EXISTING[@]} -gt 0 ]]; then
    log_warn "Following files already exist in target (will be overwritten):"
    for f in "${EXISTING[@]}"; do echo "    $f"; done
    if ! $DRY_RUN; then
        read -r -p "Overwrite? [y/N] " yn
        case "$yn" in
            [Yy]*) ;;
            *) log_info "Aborted."; exit 0 ;;
        esac
    fi
fi

# ─── 시크릿 사전 점검 ──────────────────────────────────────────────────────
log_step "Security pre-check"
log_info "Checking source files for accidental secrets..."

# 패턴: appkey/appsecret 헥스 문자열 길이 검사
declare -a SECRET_PATTERNS=(
    'appkey.*=.*"[A-Za-z0-9]{32,}"'
    'appsecret.*=.*"[A-Za-z0-9+/]{40,}"'
    'KIS_APPKEY=[A-Za-z0-9]'
    'KIS_APPSECRET=[A-Za-z0-9]'
    'access_token.*=.*"[A-Za-z0-9._-]{50,}"'
)

LEAK_FOUND=false
for f in "${FILES[@]}"; do
    for pat in "${SECRET_PATTERNS[@]}"; do
        if grep -qE "$pat" "$SOURCE_DIR/$f" 2>/dev/null; then
            log_err "POSSIBLE SECRET LEAK in $f matching: $pat"
            LEAK_FOUND=true
        fi
    done
done

if $LEAK_FOUND; then
    log_err "Aborting due to potential secret leak"
    exit 2
fi
log_ok "No secret patterns detected"

# ─── 적용 ──────────────────────────────────────────────────────────────────
log_step "Apply changes"

if $DRY_RUN; then
    log_info "DRY RUN — would copy:"
    for f in "${FILES[@]}"; do
        echo "    $SOURCE_DIR/$f → $TARGET_DIR/$f"
    done
    log_info "DRY RUN — no changes made."
    exit 0
fi

# 실제 복사
COPIED=0
for f in "${FILES[@]}"; do
    target_path="$TARGET_DIR/$f"
    target_parent="$(dirname "$target_path")"
    mkdir -p "$target_parent"
    cp "$SOURCE_DIR/$f" "$target_path"
    COPIED=$((COPIED + 1))
done
log_ok "Copied $COPIED files"

# ─── .gitignore 업데이트 (있을 때만) ────────────────────────────────────────
log_step "Update .gitignore"
GITIGNORE="$TARGET_DIR/.gitignore"
if [[ -f "$GITIGNORE" ]]; then
    declare -a GI_PATTERNS=(
        "data/approvals.sqlite"
        "data/approvals.sqlite-shm"
        "data/approvals.sqlite-wal"
        "data/_backup_pre_phase1_*/"
    )
    for pat in "${GI_PATTERNS[@]}"; do
        if ! grep -Fxq "$pat" "$GITIGNORE"; then
            echo "$pat" >> "$GITIGNORE"
            log_ok ".gitignore += $pat"
        fi
    done
else
    log_warn ".gitignore not found — skipping (please create one)"
fi

# ─── 테스트 실행 (선택) ────────────────────────────────────────────────────
log_step "Run tests"
cd "$TARGET_DIR"
if command -v python3 >/dev/null 2>&1; then
    if python3 -m pytest tests/execution/test_approval_store.py -q --tb=line \
            2>&1 | tail -5; then
        log_ok "Tests passed"
    else
        log_warn "Tests reported failures — check above output"
    fi
else
    log_warn "python3 not found — skipping test run"
fi

# ─── 다음 단계 안내 ────────────────────────────────────────────────────────
log_step "Done"
log_info "Next steps:"
echo ""
echo "  1. Read docs/MIGRATION.md (especially §2.6 — manual DB deletion)"
echo "  2. Read docs/PHASE1_CHANGES.md for the change summary"
echo "  3. Manually verify and delete old approval DB files"
echo "  4. When ready, run: ./task_phase1_push_to_github.sh"
echo ""
log_info "Phase 2 will follow in the next Claude response (after your approval)"

exit 0
