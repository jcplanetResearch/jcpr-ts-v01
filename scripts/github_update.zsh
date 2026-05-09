#!/usr/bin/env zsh
# =============================================================================
# JCPR-TS-V01 — GitHub Update Script (macOS zsh)
# Stage 2B Deliverable 1 (companion script)
#
# Purpose:
#   Safe, test-gated push of local changes to a GitHub remote, with
#   secret scanning and automatic rollback. Per <assumption> clause:
#   "No breach of security or leakage of private key, data information
#    are not allowed."
#
# Usage:
#   ./scripts/github_update.zsh                       # interactive
#   ./scripts/github_update.zsh -m "fix: foo"         # supply commit msg
#   ./scripts/github_update.zsh -n                    # dry run (no push)
#   ./scripts/github_update.zsh --skip-tests          # NOT recommended; fails closed by default
#   ./scripts/github_update.zsh -h                    # help
#
# Exit codes:
#   0   success
#   10  pre-flight failure (wrong shell, wrong dir, dirty unrelated repo)
#   20  test gate failed
#   30  path-level secret block (sensitive path staged)
#   31  content-level secret block (pattern matched in staged content)
#   40  user aborted at confirmation prompt
#   50  push failed
#   51  post-push verification failed
#   60  user interrupted (SIGINT / SIGTERM); state was rolled back
# =============================================================================

emulate -L zsh
set -e
set -u
set -o pipefail

# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

SCRIPT_NAME="github_update.zsh"
SCRIPT_VERSION="1.0.0"

# Sensitive paths that must NEVER be pushed. Anything matching these prefixes
# in the staged set causes immediate fail-closed termination.
typeset -ra BLOCKED_PATHS=(
    ".env"
    ".env.local"
    ".env.production"
    "configs/live_limited.yaml"
    "configs/capacity.yaml"
    "configs/risk_limits.yaml"
    "data/"
    "runtime/"
    "logs/"
    "secrets/"
    "*.pem"
    "*.key"
    "*.p12"
    "*.pfx"
    "id_rsa"
    "id_ed25519"
)

# Content-level secret patterns. Mirror tests/integration/conftest.py
# secret_scanner. Any single match -> fail-closed.
# Each entry: "REASON|EXTENDED_REGEX"
typeset -ra SECRET_PATTERNS=(
    "KIS_KEY_OR_SECRET|^[[:space:]]*(KIS_APP_KEY|KIS_APP_SECRET)[[:space:]]*=[[:space:]]*[^\"'[:space:]]+"
    "KIS_KEY_INLINE|(kis[_-]?app[_-]?(key|secret))[[:space:]]*[=:][[:space:]]*[\"']?[A-Za-z0-9]{20,}"
    "PSP_TOKEN|PSP[A-Z0-9]{30,}"
    "KIS_ACCOUNT_NO|[^A-Za-z0-9-][0-9]{8}-[0-9]{2}[^A-Za-z0-9-]"
    "AWS_ACCESS_KEY|AKIA[0-9A-Z]{16}"
    "GITHUB_PAT|ghp_[A-Za-z0-9]{30,}"
    "SLACK_TOKEN|xox[baprs]-[A-Za-z0-9-]{10,}"
    "PRIVATE_KEY_HEADER|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    "JCPR_INTERNAL|JCPR-INTERNAL-[A-Z0-9]{4,}"
)

# Where audit logs go. Note: this directory is itself in BLOCKED_PATHS,
# so audit logs cannot be pushed.
LOG_DIR="logs"
LOG_FILE="${LOG_DIR}/github_update_$(date +%Y%m%d_%H%M%S).log"

# Default flags
DRY_RUN=0
SKIP_TESTS=0
COMMIT_MSG=""
NONINTERACTIVE=0

# Rollback state
ROLLBACK_NEEDED=0
ROLLBACK_HEAD=""
ROLLBACK_BRANCH=""

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------

log() {
    # log <level> <message...>
    local level="$1"; shift
    local ts="$(date +%Y-%m-%dT%H:%M:%S%z)"
    local line="[${ts}] [${level}] $*"
    print -- "$line"
    if [[ -n "${LOG_FILE:-}" && -d "${LOG_FILE:h}" ]]; then
        print -- "$line" >> "$LOG_FILE"
    fi
}

info()  { log "INFO"  "$@"; }
warn()  { log "WARN"  "$@"; }
err()   { log "ERROR" "$@"; }
ok()    { log "OK"    "$@"; }

die() {
    # die <exit_code> <message>
    local code="$1"; shift
    err "$@"
    err "exit code: $code"
    exit "$code"
}

# -----------------------------------------------------------------------------
# Cleanup / Rollback handler (fires on any abnormal termination)
# -----------------------------------------------------------------------------

cleanup_on_signal() {
    local sig="$1"
    err "interrupted by signal: $sig"
    if (( ROLLBACK_NEEDED == 1 )) && [[ -n "$ROLLBACK_HEAD" ]]; then
        warn "rolling back to ${ROLLBACK_HEAD} on branch ${ROLLBACK_BRANCH}"
        git reset --hard "$ROLLBACK_HEAD" 2>/dev/null || true
    fi
    exit 60
}

trap 'cleanup_on_signal INT'  INT
trap 'cleanup_on_signal TERM' TERM

# -----------------------------------------------------------------------------
# Argument parsing
# -----------------------------------------------------------------------------

usage() {
    cat <<EOF
${SCRIPT_NAME} v${SCRIPT_VERSION} — JCPR-TS-V01 safe GitHub push

Usage:
  ${SCRIPT_NAME} [-m <msg>] [-n] [-y] [--skip-tests] [-h]

Options:
  -m <msg>        Commit message (otherwise prompts)
  -n              Dry run (run all checks, do not push)
  -y              Non-interactive (skip confirmation prompts; only safe with -n)
  --skip-tests    Skip the test gate (NOT recommended)
  -h              Show this help

Exit codes:
  0  success                        30  blocked sensitive path
  10 pre-flight failure             31  blocked secret pattern
  20 tests failed                   40  user aborted
  50 push failed                    51  post-push verification failed
  60 interrupted (rolled back)

EOF
}

parse_args() {
    while (( $# > 0 )); do
        case "$1" in
            -m) COMMIT_MSG="$2"; shift 2 ;;
            -n) DRY_RUN=1; shift ;;
            -y) NONINTERACTIVE=1; shift ;;
            --skip-tests) SKIP_TESTS=1; shift ;;
            -h|--help) usage; exit 0 ;;
            *) usage; die 10 "unknown argument: $1" ;;
        esac
    done
}

# -----------------------------------------------------------------------------
# Pre-flight checks
# -----------------------------------------------------------------------------

preflight() {
    info "pre-flight: starting"

    # 1. shell sanity
    if [[ -z "${ZSH_VERSION:-}" ]]; then
        die 10 "must be run under zsh (got: $(ps -p $$ -o comm= 2>/dev/null))"
    fi

    # 2. macOS sanity (warn, not fatal — script may work on Linux)
    if [[ "$(uname -s)" != "Darwin" ]]; then
        warn "this script targets macOS; running on $(uname -s)"
    fi

    # 3. inside a git repo
    if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        die 10 "current directory is not inside a git working tree"
    fi

    # 4. log dir
    mkdir -p "$LOG_DIR"
    chmod 700 "$LOG_DIR" 2>/dev/null || true

    # 5. branch
    ROLLBACK_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
    if [[ "$ROLLBACK_BRANCH" == "HEAD" ]]; then
        die 10 "detached HEAD state; refusing to push"
    fi
    info "branch: $ROLLBACK_BRANCH"

    # 6. remote configured
    if ! git remote get-url origin >/dev/null 2>&1; then
        die 10 "no 'origin' remote configured"
    fi
    local origin_url
    origin_url="$(git remote get-url origin)"
    info "origin: $origin_url"

    # 7. fetch latest, check for divergence
    info "fetching origin (read-only)"
    if ! git fetch --quiet origin "$ROLLBACK_BRANCH" 2>>"$LOG_FILE"; then
        warn "git fetch failed (network? auth?); continuing best-effort"
    else
        local local_head remote_head merge_base
        local_head="$(git rev-parse HEAD)"
        remote_head="$(git rev-parse "origin/${ROLLBACK_BRANCH}" 2>/dev/null || echo '')"
        if [[ -n "$remote_head" ]]; then
            merge_base="$(git merge-base HEAD "origin/${ROLLBACK_BRANCH}" 2>/dev/null || echo '')"
            if [[ "$merge_base" != "$remote_head" && "$merge_base" != "$local_head" ]]; then
                die 10 "local and origin/${ROLLBACK_BRANCH} have diverged; pull/rebase first"
            fi
        fi
    fi

    # 8. record rollback head
    ROLLBACK_HEAD="$(git rev-parse HEAD)"
    info "rollback anchor: $ROLLBACK_HEAD"

    ok "pre-flight: passed"
}

# -----------------------------------------------------------------------------
# Test gate
# -----------------------------------------------------------------------------

run_tests() {
    if (( SKIP_TESTS == 1 )); then
        warn "test gate SKIPPED via --skip-tests (not recommended)"
        return 0
    fi
    info "test gate: running pytest tests/integration/"
    if ! python -m pytest tests/integration/ -q 2>&1 | tee -a "$LOG_FILE" | tail -20; then
        die 20 "integration tests failed; refusing to push"
    fi
    ok "test gate: passed"
}

# -----------------------------------------------------------------------------
# Path-level block: refuse to even consider sensitive paths
# -----------------------------------------------------------------------------

check_blocked_paths() {
    info "path-check: scanning staged files"

    # Collect both staged AND untracked-but-modified files that would go in
    # a 'git add -A' style commit. We look at staged first (what the user
    # explicitly chose), then warn about others.
    local staged
    staged="$(git diff --name-only --cached)"

    if [[ -z "$staged" ]]; then
        # If nothing is staged yet, stage everything tracked + new (but
        # respecting .gitignore). We do this in a controlled way so we
        # can scan before committing.
        info "no staged files; staging tracked changes for scan"
        git add -A
        staged="$(git diff --name-only --cached)"
    fi

    if [[ -z "$staged" ]]; then
        die 10 "no changes to commit"
    fi

    info "staged files:"
    print -- "$staged" | sed 's/^/  /' | tee -a "$LOG_FILE" >/dev/null
    print -- "$staged" | sed 's/^/  /'

    # Match against blocked-path globs. zsh extended_glob lets us use **.
    setopt local_options extended_glob
    local file
    local -a hits=()
    while IFS= read -r file; do
        [[ -z "$file" ]] && continue
        local pattern
        for pattern in "${BLOCKED_PATHS[@]}"; do
            case "$file" in
                ${~pattern}|${~pattern}/*|*/${~pattern}|*/${~pattern}/*)
                    hits+="$file (matches blocked pattern: $pattern)"
                    ;;
            esac
        done
    done <<< "$staged"

    if (( ${#hits[@]} > 0 )); then
        err "BLOCKED: sensitive path(s) staged for commit:"
        for h in "${hits[@]}"; do err "  - $h"; done
        err "unstage these paths and ensure they are in .gitignore"
        die 30 "path-level secret block"
    fi

    ok "path-check: no sensitive paths staged"
}

# -----------------------------------------------------------------------------
# Content-level scan: regex patterns on staged diff content
# -----------------------------------------------------------------------------

check_secret_patterns() {
    info "content-scan: scanning staged diff for secret patterns"

    # Get the staged content (additions only — '^+' lines), excluding the
    # diff header lines. We feed THIS to the regex engine, not the literal
    # files, so we only flag what's about to enter the repo.
    local staged_diff
    staged_diff="$(git diff --cached --no-color -U0)"

    if [[ -z "$staged_diff" ]]; then
        die 10 "git diff --cached returned empty; nothing to scan"
    fi

    # Build a temp file of only the added lines (drop diff metadata)
    local tmp
    tmp="$(mktemp -t jcpr_scan.XXXXXX)"
    # shellcheck disable=SC2064
    trap "rm -f '$tmp'" EXIT

    print -- "$staged_diff" \
        | awk '
            /^diff --git/      { fn=$0; next }
            /^\+\+\+ /         { fn=$0; next }
            /^--- /            { next }
            /^@@/              { hdr=$0; next }
            /^\+/              {
                # strip leading "+"
                line=$0; sub(/^\+/, "", line)
                print FILENAME":"NR":"line
            }
        ' > "$tmp" || true

    # Per-pattern grep. We deliberately do not echo the matched line content
    # to logs; only the file path and pattern reason are logged.
    local -a violations=()
    local entry reason regex
    for entry in "${SECRET_PATTERNS[@]}"; do
        reason="${entry%%|*}"
        regex="${entry#*|}"
        # use grep -E for ERE; -n for line number; suppress content from log
        local hit_count
        hit_count="$(grep -E -c -- "$regex" "$tmp" 2>/dev/null || true)"
        if [[ -n "$hit_count" && "$hit_count" -gt 0 ]]; then
            violations+="$reason ($hit_count hit(s))"
        fi
    done

    rm -f "$tmp"; trap - EXIT

    if (( ${#violations[@]} > 0 )); then
        err "BLOCKED: secret pattern(s) detected in staged content:"
        for v in "${violations[@]}"; do err "  - $v"; done
        err "remove these from the staged changes; do NOT log the matched values."
        die 31 "content-level secret block"
    fi

    ok "content-scan: no secret patterns matched"
}

# -----------------------------------------------------------------------------
# Confirmation
# -----------------------------------------------------------------------------

confirm_or_die() {
    if (( DRY_RUN == 1 )); then
        info "dry run: would commit + push at this point"
        return 0
    fi
    if (( NONINTERACTIVE == 1 )); then
        die 10 "-y is only valid with -n (dry run)"
    fi

    print -- ""
    print -- "About to commit and push to origin/${ROLLBACK_BRANCH}."
    print -- "Files:"
    git diff --name-only --cached | sed 's/^/  /'
    print -- ""
    print -n "Type 'yes' to proceed: "
    local answer
    read -r answer
    if [[ "$answer" != "yes" ]]; then
        warn "user declined; aborting (no commit, no push)"
        # un-stage everything we auto-staged so we leave the tree as we found it
        git reset HEAD -- . >/dev/null 2>&1 || true
        exit 40
    fi
}

# -----------------------------------------------------------------------------
# Commit + push + verify
# -----------------------------------------------------------------------------

do_commit_and_push() {
    if [[ -z "$COMMIT_MSG" ]]; then
        if (( NONINTERACTIVE == 1 )) || (( DRY_RUN == 1 )); then
            COMMIT_MSG="chore: jcpr automated update"
        else
            print -n "Commit message: "
            read -r COMMIT_MSG
        fi
    fi
    if [[ -z "$COMMIT_MSG" ]]; then
        die 10 "empty commit message"
    fi

    if (( DRY_RUN == 1 )); then
        info "dry run: skipping commit + push"
        return 0
    fi

    info "committing: $COMMIT_MSG"
    if ! git commit -m "$COMMIT_MSG" 2>&1 | tee -a "$LOG_FILE"; then
        die 50 "git commit failed"
    fi
    ROLLBACK_NEEDED=1

    info "pushing to origin/${ROLLBACK_BRANCH}"
    if ! git push origin "$ROLLBACK_BRANCH" 2>&1 | tee -a "$LOG_FILE"; then
        warn "push failed; rolling back local commit"
        git reset --hard "$ROLLBACK_HEAD"
        ROLLBACK_NEEDED=0
        die 50 "git push failed; local state restored"
    fi

    # Post-push verification: remote HEAD must equal local HEAD
    info "verifying remote HEAD"
    git fetch --quiet origin "$ROLLBACK_BRANCH"
    local local_head remote_head
    local_head="$(git rev-parse HEAD)"
    remote_head="$(git rev-parse "origin/${ROLLBACK_BRANCH}")"
    if [[ "$local_head" != "$remote_head" ]]; then
        err "post-push verification FAILED:"
        err "  local : $local_head"
        err "  remote: $remote_head"
        die 51 "remote HEAD does not match local; investigate manually"
    fi
    ok "remote HEAD matches: $local_head"
    ROLLBACK_NEEDED=0
}

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

main() {
    parse_args "$@"
    mkdir -p "$LOG_DIR"
    info "${SCRIPT_NAME} v${SCRIPT_VERSION} starting (pid $$)"
    info "log file: $LOG_FILE"

    preflight
    run_tests
    check_blocked_paths
    check_secret_patterns
    confirm_or_die
    do_commit_and_push

    ok "done."
}

main "$@"
